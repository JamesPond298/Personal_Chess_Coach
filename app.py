from __future__ import annotations

import io
import json
import os
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path

import chess
import chess.engine
import chess.pgn
import requests
from flask import Flask, jsonify, request, send_from_directory

APP_DIR = Path(__file__).resolve().parent


def get_user_data_dir():
    """
    Store user data outside the application folder so upgrades do not
    overwrite or require copying learning history.

    Windows:
      %LOCALAPPDATA%\\PersonalChessCoach

    macOS:
      ~/Library/Application Support/PersonalChessCoach

    Linux:
      ~/.local/share/PersonalChessCoach
    """
    if os.name == "nt":
        base = os.getenv("LOCALAPPDATA")
        if base:
            return Path(base) / "PersonalChessCoach"
        return Path.home() / "AppData" / "Local" / "PersonalChessCoach"

    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "PersonalChessCoach"
        )

    xdg = os.getenv("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "PersonalChessCoach"

    return (
        Path.home()
        / ".local"
        / "share"
        / "PersonalChessCoach"
    )


DATA_DIR = get_user_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_FILE = DATA_DIR / "config.json"
LEARNING_FILE = DATA_DIR / "learning.json"

LEGACY_DATA_DIR = APP_DIR / "data"


def migrate_legacy_data():
    """
    One-time migration from older versions that stored data beside app.py.
    Existing files in the new permanent location are never overwritten.
    """
    if not LEGACY_DATA_DIR.exists():
        return

    for filename in ("config.json", "learning.json"):
        old_file = LEGACY_DATA_DIR / filename
        new_file = DATA_DIR / filename

        if old_file.exists() and not new_file.exists():
            try:
                shutil.copy2(old_file, new_file)
                print(
                    f"Migrated {filename} to permanent data location: "
                    f"{new_file}"
                )
            except Exception as e:
                print(
                    f"Could not migrate {filename}: {e}"
                )


migrate_legacy_data()

app = Flask(__name__, static_folder="static")

ACTIVE_ENGINE = None
ENGINE_LOCK = threading.Lock()
ANALYSIS_JOBS = {}

CHESSCOM_HEADERS = {
    "User-Agent": "PersonalChessCoach/0.8 (local postgame analysis tool)"
}


def load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"stockfish_path": ""}


def save_config(config):
    CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")


def find_stockfish():
    cfg = load_config()
    candidates = [
        cfg.get("stockfish_path", ""),
        os.getenv("STOCKFISH_PATH", ""),
        shutil.which("stockfish") or "",
        shutil.which("stockfish.exe") or "",
        str(APP_DIR / "stockfish.exe"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.exists() and path.is_file() and path.suffix.lower() == ".exe":
            return str(path.resolve())
    return None


def load_learning():
    if LEARNING_FILE.exists():
        try:
            data = json.loads(LEARNING_FILE.read_text(encoding="utf-8"))
            data.setdefault("games", [])
            data.setdefault("mistakes", [])
            data.setdefault("training", [])
            data.setdefault("feedback", [])
            return data
        except Exception:
            pass

    return {
        "games": [],
        "mistakes": [],
        "training": [],
        "feedback": [],
    }


def save_learning(data):
    LEARNING_FILE.write_text(
        json.dumps(data, indent=2),
        encoding="utf-8"
    )


def mistake_category(review):
    classification = review.get("classification", "")
    played = review.get("playedSan", "")
    best = review.get("bestSan", "")
    text = (review.get("coach", {}) or {}).get("explanation", "").lower()

    if "capture" in text:
        return "Capture Decision"
    if "check" in text:
        return "Forcing Move"
    if "king safety" in text or "castle" in text:
        return "King Safety"
    if classification == "Blunder":
        return "Tactical Oversight"
    if classification == "Mistake":
        return "Calculation / Threat Recognition"
    if classification == "Inaccuracy":
        return "Move Precision"
    return "General"


def schedule_days(box):
    # Lightweight Leitner-style spacing.
    return [1, 3, 7, 14, 30][min(max(box, 0), 4)]


def record_game_learning(result):
    data = load_learning()

    headers = result.get("headers", {})
    game_key = "|".join([
        headers.get("White", ""),
        headers.get("Black", ""),
        headers.get("Date", ""),
        headers.get("Result", ""),
        str(result.get("depth", "")),
    ])

    if any(g.get("key") == game_key for g in data["games"]):
        return

    now = int(time.time())

    data["games"].append({
        "key": game_key,
        "white": headers.get("White", ""),
        "black": headers.get("Black", ""),
        "date": headers.get("Date", ""),
        "result": headers.get("Result", ""),
        "accuracy": result.get("accuracyEstimate"),
        "counts": result.get("counts", {}),
        "created": now,
    })

    for review in result.get("reviews", []):
        if review.get("classification") not in ("Inaccuracy", "Mistake", "Blunder"):
            continue

        mistake_id = uuid.uuid4().hex
        category = mistake_category(review)

        item = {
            "id": mistake_id,
            "gameKey": game_key,
            "ply": review.get("ply"),
            "moveNumber": review.get("moveNumber"),
            "color": review.get("color"),
            "playedSan": review.get("playedSan"),
            "bestSan": review.get("bestSan"),
            "bestUci": review.get("bestUci"),
            "fenBefore": review.get("fenBefore"),
            "fenAfter": review.get("fenAfter"),
            "classification": review.get("classification"),
            "lossCp": review.get("lossCp", 0),
            "category": category,
            "coach": review.get("coach", {}),
            "thought": "",
            "thoughtTag": "",
            "created": now,
        }

        data["mistakes"].append(item)

        data["training"].append({
            "mistakeId": mistake_id,
            "box": 0,
            "attempts": 0,
            "correct": 0,
            "lastSeen": 0,
            "nextDue": now,
            "retired": False,
        })

    save_learning(data)



def mastery_from_learning(data):
    stats = {}

    mistake_map = {
        m.get("id"): m
        for m in data.get("mistakes", [])
    }

    for t in data.get("training", []):
        mistake = mistake_map.get(t.get("mistakeId"))
        if not mistake:
            continue

        concept = mistake.get("category", "General")
        item = stats.setdefault(concept, {
            "concept": concept,
            "attempts": 0,
            "correct": 0,
            "boxTotal": 0,
            "items": 0,
        })

        item["attempts"] += t.get("attempts", 0)
        item["correct"] += t.get("correct", 0)
        item["boxTotal"] += t.get("box", 0)
        item["items"] += 1

    results = []
    for concept, item in stats.items():
        attempts = item["attempts"]
        accuracy = (item["correct"] / attempts * 100) if attempts else 0
        spacing = (item["boxTotal"] / max(item["items"], 1)) / 4 * 100

        # Blend actual solving accuracy with spaced-repetition progress.
        score = round(accuracy * 0.7 + spacing * 0.3)
        results.append({
            "concept": concept,
            "score": max(0, min(100, score)),
            "attempts": attempts,
            "correct": item["correct"],
        })

    results.sort(key=lambda x: (x["score"], -x["attempts"]))
    return results


def progress_dashboard(data):
    games = data.get("games", [])
    mistakes = data.get("mistakes", [])
    training = data.get("training", [])
    feedback = data.get("feedback", [])

    def block_stats(block):
        if not block:
            return {
                "games": 0,
                "blundersPerGame": 0,
                "mistakesPerGame": 0,
                "inaccuraciesPerGame": 0,
                "accuracy": 0,
            }

        n = len(block)
        return {
            "games": n,
            "blundersPerGame": round(sum(g.get("counts", {}).get("Blunder", 0) for g in block) / n, 2),
            "mistakesPerGame": round(sum(g.get("counts", {}).get("Mistake", 0) for g in block) / n, 2),
            "inaccuraciesPerGame": round(sum(g.get("counts", {}).get("Inaccuracy", 0) for g in block) / n, 2),
            "accuracy": round(sum(float(g.get("accuracy") or 0) for g in block) / n, 1),
        }

    baseline = block_stats(games[:10])
    recent = block_stats(games[-10:])

    total_attempts = sum(t.get("attempts", 0) for t in training)
    total_correct = sum(t.get("correct", 0) for t in training)
    repeat_items = [t for t in training if t.get("attempts", 0) >= 2]
    repeat_attempts = sum(t.get("attempts", 0) for t in repeat_items)
    repeat_correct = sum(t.get("correct", 0) for t in repeat_items)

    retention = round((repeat_correct / repeat_attempts * 100), 1) if repeat_attempts else 0
    training_accuracy = round((total_correct / total_attempts * 100), 1) if total_attempts else 0

    understanding = {
        "I understand this": 0,
        "Sort of": 0,
        "I still don't get it": 0,
    }
    for f in feedback:
        rating = f.get("rating")
        if rating in understanding:
            understanding[rating] += 1

    mastery = mastery_from_learning(data)

    current_priority = "Review more games to establish your baseline."
    if mastery:
        current_priority = mastery[0]["concept"]
    elif mistakes:
        counts = {}
        for m in mistakes:
            c = m.get("category", "General")
            counts[c] = counts.get(c, 0) + 1
        current_priority = max(counts, key=counts.get)

    milestone = "Baseline forming"
    if len(games) >= 100:
        milestone = "100-game transfer checkpoint"
    elif len(games) >= 50:
        milestone = "50-game behavior checkpoint"
    elif len(games) >= 25:
        milestone = "25-game recognition checkpoint"
    elif len(games) >= 10:
        milestone = "10-game baseline complete"

    blunder_change = None
    if baseline["games"] and recent["games"] and baseline["blundersPerGame"]:
        blunder_change = round(
            (baseline["blundersPerGame"] - recent["blundersPerGame"])
            / baseline["blundersPerGame"] * 100,
            1
        )

    return {
        "gamesReviewed": len(games),
        "milestone": milestone,
        "baseline": baseline,
        "recent": recent,
        "blunderImprovementPercent": blunder_change,
        "training": {
            "attempts": total_attempts,
            "accuracy": training_accuracy,
            "retention": retention,
            "repeatItems": len(repeat_items),
        },
        "understanding": understanding,
        "mastery": mastery[:10],
        "currentPriority": current_priority,
        "nextCheckpoint": (
            10 if len(games) < 10
            else 25 if len(games) < 25
            else 50 if len(games) < 50
            else 100 if len(games) < 100
            else len(games) + 25
        ),
    }

def profile_from_learning(data):
    categories = {}
    classifications = {"Inaccuracy": 0, "Mistake": 0, "Blunder": 0}
    thought_tags = {}

    for item in data.get("mistakes", []):
        category = item.get("category", "General")
        categories[category] = categories.get(category, 0) + 1

        c = item.get("classification")
        if c in classifications:
            classifications[c] += 1

        tag = item.get("thoughtTag")
        if tag:
            thought_tags[tag] = thought_tags.get(tag, 0) + 1

    sorted_categories = sorted(
        categories.items(),
        key=lambda x: x[1],
        reverse=True
    )

    sorted_thoughts = sorted(
        thought_tags.items(),
        key=lambda x: x[1],
        reverse=True
    )

    due = 0
    now = int(time.time())
    for t in data.get("training", []):
        if not t.get("retired") and t.get("nextDue", 0) <= now:
            due += 1

    return {
        "gamesReviewed": len(data.get("games", [])),
        "mistakesLogged": len(data.get("mistakes", [])),
        "dueTraining": due,
        "classifications": classifications,
        "categories": [
            {"name": name, "count": count}
            for name, count in sorted_categories[:8]
        ],
        "thoughtPatterns": [
            {"name": name, "count": count}
            for name, count in sorted_thoughts[:8]
        ],
    }


def split_pgn_games(pgn_text: str):
    games = []
    stream = io.StringIO(pgn_text)
    while True:
        game = chess.pgn.read_game(stream)
        if game is None:
            break
        games.append(game)
    return games


def game_to_summary(game, source=None, url=None):
    h = game.headers
    return {
        "source": source or h.get("Site", "PGN"),
        "url": url or h.get("Link") or h.get("Site"),
        "event": h.get("Event", "Game"),
        "date": h.get("UTCDate") or h.get("Date", ""),
        "time": h.get("UTCTime", ""),
        "white": h.get("White", "White"),
        "whiteElo": h.get("WhiteElo", ""),
        "black": h.get("Black", "Black"),
        "blackElo": h.get("BlackElo", ""),
        "result": h.get("Result", "*"),
        "timeControl": h.get("TimeControl", ""),
        "pgn": str(game),
    }


def parse_pgn_text(pgn_text):
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if not game:
        raise ValueError("Could not parse this PGN.")

    board = game.board()
    positions = [{
        "ply": 0,
        "fen": board.fen(),
        "san": None,
        "uci": None,
        "turn": "white" if board.turn else "black",
    }]
    moves = []
    node = game
    ply = 0

    while node.variations:
        nxt = node.variation(0)
        move = nxt.move
        san = board.san(move)
        board.push(move)
        ply += 1
        moves.append({"ply": ply, "san": san, "uci": move.uci(), "fen": board.fen()})
        positions.append({
            "ply": ply,
            "fen": board.fen(),
            "san": san,
            "uci": move.uci(),
            "turn": "white" if board.turn else "black",
        })
        node = nxt

    return game, moves, positions


def normalize_score(score, pov_color):
    value = score.pov(pov_color).score(mate_score=100000)
    return int(value if value is not None else 0)


def cp_to_eval(cp):
    if abs(cp) >= 90000:
        return 999 if cp > 0 else -999
    return round(cp / 100, 2)


def classify_loss(loss_cp):
    if loss_cp <= 12:
        return "Best"
    if loss_cp <= 30:
        return "Excellent"
    if loss_cp <= 60:
        return "Good"
    if loss_cp <= 110:
        return "Inaccuracy"
    if loss_cp <= 220:
        return "Mistake"
    return "Blunder"


def board_features(board, move):
    features = []
    if board.is_capture(move):
        features.append("capture")
    if board.is_castling(move):
        features.append("castle")
    if move.promotion:
        features.append("promotion")
    board.push(move)
    if board.is_check():
        features.append("check")
    if board.is_checkmate():
        features.append("checkmate")
    board.pop()
    return features



PIECE_VALUES = {
    chess.PAWN: 1,
    chess.KNIGHT: 3,
    chess.BISHOP: 3,
    chess.ROOK: 5,
    chess.QUEEN: 9,
    chess.KING: 0,
}


def material_score(board, color):
    total = 0
    for piece_type, value in PIECE_VALUES.items():
        total += len(board.pieces(piece_type, color)) * value
    return total


def developed_minor_count(board, color):
    if color == chess.WHITE:
        starting = {
            chess.B1: chess.KNIGHT,
            chess.G1: chess.KNIGHT,
            chess.C1: chess.BISHOP,
            chess.F1: chess.BISHOP,
        }
    else:
        starting = {
            chess.B8: chess.KNIGHT,
            chess.G8: chess.KNIGHT,
            chess.C8: chess.BISHOP,
            chess.F8: chess.BISHOP,
        }

    undeveloped = 0
    for square, piece_type in starting.items():
        piece = board.piece_at(square)
        if piece and piece.color == color and piece.piece_type == piece_type:
            undeveloped += 1

    return 4 - undeveloped


def center_control_count(board, color):
    squares = [chess.D4, chess.E4, chess.D5, chess.E5]
    return sum(1 for sq in squares if board.is_attacked_by(color, sq))


def loose_pieces(board, color):
    loose = []
    for sq in chess.SQUARES:
        piece = board.piece_at(sq)
        if not piece or piece.color != color or piece.piece_type == chess.KING:
            continue

        defenders = len(board.attackers(color, sq))
        attackers = len(board.attackers(not color, sq))

        if attackers > defenders:
            loose.append(chess.square_name(sq))

    return loose


def king_safety_label(board, color):
    king_sq = board.king(color)
    if king_sq is None:
        return "Unknown"

    rank = chess.square_rank(king_sq)
    file_ = chess.square_file(king_sq)

    castled_files = (2, 6)
    home_rank = 0 if color == chess.WHITE else 7

    if rank == home_rank and file_ in castled_files:
        return "Good"

    if board.has_castling_rights(color):
        return "Can still castle"

    enemy_attackers = len(board.attackers(not color, king_sq))
    if enemy_attackers:
        return "Under pressure"

    return "Uncastled / exposed"


def position_teaching_summary(board):
    white_material = material_score(board, chess.WHITE)
    black_material = material_score(board, chess.BLACK)

    white_dev = developed_minor_count(board, chess.WHITE)
    black_dev = developed_minor_count(board, chess.BLACK)

    white_center = center_control_count(board, chess.WHITE)
    black_center = center_control_count(board, chess.BLACK)

    white_loose = loose_pieces(board, chess.WHITE)
    black_loose = loose_pieces(board, chess.BLACK)

    side = board.turn

    if white_material > black_material:
        material = f"White is ahead by {white_material - black_material} point(s) of material."
    elif black_material > white_material:
        material = f"Black is ahead by {black_material - white_material} point(s) of material."
    else:
        material = "Material is equal."

    if white_dev > black_dev:
        development = "White has more minor pieces developed."
    elif black_dev > white_dev:
        development = "Black has more minor pieces developed."
    else:
        development = "Development is roughly even."

    if white_center > black_center:
        center = "White currently controls more of the four central squares."
    elif black_center > white_center:
        center = "Black currently controls more of the four central squares."
    else:
        center = "Central control is roughly balanced."

    side_loose = white_loose if side == chess.WHITE else black_loose

    if side_loose:
        threats = (
            "Before looking for a plan, check these potentially loose pieces: "
            + ", ".join(side_loose[:4]) + "."
        )
    else:
        threats = "No obvious loose piece stands out for the side to move."

    return {
        "material": material,
        "development": development,
        "center": center,
        "whiteKingSafety": king_safety_label(board, chess.WHITE),
        "blackKingSafety": king_safety_label(board, chess.BLACK),
        "looseWhite": white_loose,
        "looseBlack": black_loose,
        "threats": threats,
    }


def move_teaching(board, move):
    if move is None:
        return {
            "concept": "No move",
            "why": "No engine move was available.",
            "accomplishes": "",
            "nextPlan": "",
            "question": "",
        }

    san = board.san(move)
    piece = board.piece_at(move.from_square)
    features = board_features(board, move)

    concept = "Piece Improvement"
    why = f"{san} improves the position without creating an obvious weakness."
    accomplishes = "It improves coordination and keeps useful options available."
    next_plan = "After this move, continue improving your least active piece and watch for tactical opportunities."
    question = "Which of your pieces is doing the least right now?"

    if "checkmate" in features:
        concept = "Checkmate"
        why = f"{san} ends the game immediately."
        accomplishes = "It delivers checkmate."
        next_plan = "No further plan is needed."
        question = "What squares prevent the king from escaping?"

    elif "check" in features:
        concept = "Forcing Move"
        why = f"{san} gives check, forcing the opponent to respond."
        accomplishes = "It takes away the opponent's freedom to play whatever they want."
        next_plan = "After the forced reply, look for another forcing move or a way to improve your pieces."
        question = "After the opponent answers the check, what checks, captures, or threats remain?"

    elif "capture" in features:
        concept = "Capture / Material"
        why = f"{san} uses a capture to change the material or remove an important defender."
        accomplishes = "It simplifies or wins material while solving an immediate tactical issue."
        next_plan = "Recount the material afterward and make sure the captured piece cannot be regained tactically."
        question = "After this capture, who has more material and what can the opponent recapture?"

    elif "castle" in features:
        concept = "King Safety"
        why = f"{san} gets the king safer and connects the rook to the rest of the game."
        accomplishes = "It improves king safety and helps complete development."
        next_plan = "Now bring your remaining undeveloped pieces into the game and connect the rooks."
        question = "Which piece still needs to be developed after you castle?"

    elif piece and piece.piece_type in (chess.KNIGHT, chess.BISHOP):
        starting_rank = 0 if piece.color == chess.WHITE else 7
        if chess.square_rank(move.from_square) == starting_rank:
            concept = "Development"
            why = f"{san} develops a minor piece from its starting square."
            accomplishes = "It brings another piece into the fight, usually toward the center."
            next_plan = "Keep developing, fight for the center, and prepare to castle."
            question = "Which other minor piece is still on its starting square?"

    if move.to_square in (chess.D4, chess.E4, chess.D5, chess.E5):
        concept = "Center Control"
        why = f"{san} puts direct influence into the center."
        accomplishes = "Central control gives your pieces more space and limits the opponent."
        next_plan = "Use the central foothold to develop pieces with tempo and keep your king safe."
        question = "Which central squares do you control after this move?"

    return {
        "concept": concept,
        "why": why,
        "accomplishes": accomplishes,
        "nextPlan": next_plan,
        "question": question,
    }


def explain_pv(board, pv_moves):
    explanations = []
    pv_board = board.copy(stack=False)

    for index, move in enumerate(pv_moves[:6]):
        try:
            san = pv_board.san(move)
            teaching = move_teaching(pv_board, move)
            explanations.append({
                "ply": index + 1,
                "san": san,
                "concept": teaching["concept"],
                "why": teaching["why"],
            })
            pv_board.push(move)
        except Exception:
            break

    return explanations

def coach_text(board_before, played_move, best_move, loss_cp, classification):
    played_san = board_before.san(played_move)
    best_san = board_before.san(best_move) if best_move else ""
    played_features = board_features(board_before, played_move)
    best_features = board_features(board_before, best_move) if best_move else []

    if classification in ("Best", "Excellent"):
        reason = "Your move stayed very close to Stockfish's preferred continuation."
        if "capture" in played_features:
            reason = "Your capture was tactically sound and kept the position under control."
        elif "check" in played_features:
            reason = "Your check created useful pressure without allowing a strong reply."
        lesson = "Identify what the move improved: material, activity, king safety, or a concrete threat."
    elif classification == "Good":
        reason = "Your move was playable, but Stockfish found a more precise continuation."
        lesson = "When two moves look reasonable, compare the opponent's best reply to each one."
    else:
        if loss_cp >= 220:
            reason = "This was the major turning point: the evaluation changed sharply after your move."
        elif loss_cp >= 110:
            reason = "This gave away a noticeable amount of the position and allowed stronger counterplay."
        else:
            reason = "This was reasonable, but it gave up some of your position's potential."

        if best_move:
            if "capture" in best_features:
                reason += f" Stockfish preferred {best_san}, using a capture to address the position immediately."
            elif "check" in best_features:
                reason += f" Stockfish preferred {best_san}, a forcing check."
            elif "castle" in best_features:
                reason += f" Stockfish preferred {best_san}, improving king safety."
            else:
                reason += f" Stockfish preferred {best_san}."

        lesson = "Before committing, scan checks, captures, threats, and what your opponent is attacking."

    best_teaching = move_teaching(board_before, best_move) if best_move else {
        "concept": "General",
        "why": "",
        "accomplishes": "",
        "nextPlan": "",
        "question": "",
    }

    return {
        "headline": f"{played_san} — {classification}",
        "explanation": reason,
        "lesson": lesson,
        "question": best_teaching.get("question") or "What is the opponent's strongest forcing reply after your move?",
        "concept": best_teaching.get("concept", "General"),
        "whyBest": best_teaching.get("why", ""),
        "accomplishes": best_teaching.get("accomplishes", ""),
        "nextPlan": best_teaching.get("nextPlan", ""),
    }


def analyze_game_core(pgn_text, username, depth, progress_callback=None):
    global ACTIVE_ENGINE

    stockfish_path = find_stockfish()
    if not stockfish_path:
        raise RuntimeError("Stockfish was not found. Open Settings and select the actual stockfish.exe file.")

    game, _, _ = parse_pgn_text(pgn_text)
    headers = game.headers
    white_name = headers.get("White", "").lower()
    black_name = headers.get("Black", "").lower()

    if username:
        if username == white_name:
            user_color = chess.WHITE
        elif username == black_name:
            user_color = chess.BLACK
        else:
            raise ValueError(f"Username '{username}' is not White or Black in this PGN.")
    else:
        user_color = None

    # Count moves that will actually be reviewed.
    count_board = game.board()
    count_node = game
    total_reviewed = 0
    while count_node.variations:
        nxt = count_node.variation(0)
        mover = count_board.turn
        if user_color is None or mover == user_color:
            total_reviewed += 1
        count_board.push(nxt.move)
        count_node = nxt

    engine = None
    reviews = []
    completed = 0

    with ENGINE_LOCK:
        try:
            engine = chess.engine.SimpleEngine.popen_uci(stockfish_path)
            ACTIVE_ENGINE = engine

            board = game.board()
            node = game

            while node.variations:
                nxt = node.variation(0)
                move = nxt.move
                mover = board.turn

                before = engine.analyse(board, chess.engine.Limit(depth=depth))
                best_line = before.get("pv", [])
                best_move = best_line[0] if best_line else None
                best_cp_for_mover = normalize_score(before["score"], mover)
                best_eval_white = normalize_score(before["score"], chess.WHITE)

                played_san = board.san(move)
                board_before = board.copy(stack=False)
                board.push(move)

                after = engine.analyse(board, chess.engine.Limit(depth=depth))
                played_cp_for_mover = normalize_score(after["score"], mover)
                eval_white_after = normalize_score(after["score"], chess.WHITE)

                loss_cp = max(0, best_cp_for_mover - played_cp_for_mover)
                classification = classify_loss(loss_cp)

                pv_san = []
                pv_board = board_before.copy(stack=False)
                for pv_move in before.get("pv", [])[:6]:
                    try:
                        pv_san.append(pv_board.san(pv_move))
                        pv_board.push(pv_move)
                    except Exception:
                        break

                if user_color is None or mover == user_color:
                    coach = coach_text(board_before, move, best_move, loss_cp, classification)
                    best_san = ""
                    if best_move:
                        try:
                            best_san = board_before.san(best_move)
                        except Exception:
                            pass

                    reviews.append({
                        "ply": board.ply(),
                        "moveNumber": (board.ply() + 1) // 2,
                        "color": "white" if mover else "black",
                        "playedSan": played_san,
                        "playedUci": move.uci(),
                        "bestSan": best_san,
                        "bestUci": best_move.uci() if best_move else "",
                        "evalBefore": cp_to_eval(best_eval_white),
                        "evalAfter": cp_to_eval(eval_white_after),
                        "lossCp": loss_cp,
                        "classification": classification,
                        "pv": pv_san,
                        "fenBefore": board_before.fen(),
                        "fenAfter": board.fen(),
                        "coach": coach,
                        "positionTeaching": position_teaching_summary(board_before),
                        "pvTeaching": explain_pv(board_before, before.get("pv", [])),
                    })

                    completed += 1
                    if progress_callback:
                        progress_callback(completed, max(total_reviewed, 1), played_san)

                node = nxt

            counts = {k: 0 for k in ["Best", "Excellent", "Good", "Inaccuracy", "Mistake", "Blunder"]}
            for review in reviews:
                counts[review["classification"]] += 1

            weighted_loss = sum(min(review["lossCp"], 500) for review in reviews)
            accuracy = max(
                0.0,
                min(100.0, 100.0 - (weighted_loss / max(len(reviews), 1)) / 3.2)
            )

            critical = sorted(reviews, key=lambda item: item["lossCp"], reverse=True)[:5]
            themes = []
            if counts["Blunder"] >= 1:
                themes.append("Tactical safety: scan checks, captures, and threats before committing.")
            if counts["Mistake"] + counts["Inaccuracy"] >= max(2, len(reviews) // 5):
                themes.append("Candidate moves: compare at least two reasonable moves before deciding.")
            if not themes:
                themes.append("Consistency: your major decisions stayed relatively close to Stockfish's choices.")

            return {
                "headers": dict(headers),
                "userColor": (
                    "white" if user_color is chess.WHITE
                    else "black" if user_color is chess.BLACK
                    else "both"
                ),
                "depth": depth,
                "accuracyEstimate": round(accuracy, 1),
                "counts": counts,
                "criticalMoments": critical,
                "themes": themes,
                "reviews": reviews,
            }

        finally:
            if engine is not None:
                try:
                    engine.quit()
                except Exception:
                    try:
                        engine.close()
                    except Exception:
                        pass
            ACTIVE_ENGINE = None


def run_analysis_job(job_id, pgn_text, username, depth):
    job = ANALYSIS_JOBS[job_id]
    try:
        def update(done, total, move_san):
            job["status"] = "running"
            job["done"] = done
            job["total"] = total
            job["percent"] = round((done / total) * 100)
            job["message"] = f"Reviewed {done} of {total} moves — latest: {move_san}"

        result = analyze_game_core(pgn_text, username, depth, update)
        record_game_learning(result)
        job["result"] = result
        job["percent"] = 100
        job["status"] = "complete"
        job["message"] = "Analysis complete."
    except Exception as e:
        job["status"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/api/status")
def status():
    path = find_stockfish()
    return jsonify({
        "stockfishFound": bool(path),
        "stockfishPath": path or "",
        "version": "0.8.0",
        "engineBusy": ACTIVE_ENGINE is not None,
        "dataDirectory": str(DATA_DIR),
        "learningFile": str(LEARNING_FILE),
    })


@app.post("/api/settings/stockfish")
def set_stockfish():
    body = request.get_json(force=True)
    raw_path = body.get("path", "").strip().strip('"')
    path = Path(raw_path) if raw_path else None

    if not path or not path.exists() or not path.is_file() or path.suffix.lower() != ".exe":
        return jsonify({"error": "Please select the actual Stockfish .exe file, not its folder."}), 400

    cfg = load_config()
    cfg["stockfish_path"] = str(path.resolve())
    save_config(cfg)
    return jsonify({"ok": True, "path": cfg["stockfish_path"]})


@app.get("/api/games/chesscom/<username>")
def chesscom_games(username):
    limit = min(max(int(request.args.get("limit", 30)), 1), 100)
    archives_url = f"https://api.chess.com/pub/player/{username}/games/archives"
    r = requests.get(archives_url, headers=CHESSCOM_HEADERS, timeout=20)
    if r.status_code == 404:
        return jsonify({"error": "Chess.com username not found."}), 404
    r.raise_for_status()

    results = []
    for archive in reversed(r.json().get("archives", [])[-4:]):
        rr = requests.get(archive, headers=CHESSCOM_HEADERS, timeout=20)
        if not rr.ok:
            continue
        for g in reversed(rr.json().get("games", [])):
            pgn = g.get("pgn")
            if not pgn or g.get("rules") != "chess":
                continue
            try:
                game = chess.pgn.read_game(io.StringIO(pgn))
                if not game:
                    continue
                item = game_to_summary(game, "Chess.com", g.get("url"))
                item["endTime"] = g.get("end_time")
                item["timeClass"] = g.get("time_class", "")
                results.append(item)
                if len(results) >= limit:
                    return jsonify(results)
            except Exception:
                continue
    return jsonify(results)


@app.get("/api/games/lichess/<username>")
def lichess_games(username):
    limit = min(max(int(request.args.get("limit", 30)), 1), 100)
    r = requests.get(
        f"https://lichess.org/api/games/user/{username}",
        headers={
            "Accept": "application/x-chess-pgn",
            "User-Agent": "PersonalChessCoach/0.8",
        },
        params={
            "max": limit,
            "moves": "true",
            "tags": "true",
            "clocks": "true",
            "evals": "false",
            "opening": "true",
        },
        timeout=30,
    )
    if r.status_code == 404:
        return jsonify({"error": "Lichess username not found."}), 404
    r.raise_for_status()
    return jsonify([game_to_summary(g, "Lichess") for g in split_pgn_games(r.text)])


@app.post("/api/pgn/parse")
def parse_pgn():
    body = request.get_json(force=True)
    try:
        game, moves, positions = parse_pgn_text(body.get("pgn", ""))
        return jsonify({
            "headers": dict(game.headers),
            "moves": moves,
            "positions": positions,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.post("/api/analyze/start")
def analyze_start():
    body = request.get_json(force=True)
    pgn_text = body.get("pgn", "")
    username = (body.get("username") or "").strip().lower()
    depth = min(max(int(body.get("depth", 15)), 8), 22)

    # Parse before creating job so obvious problems return immediately.
    try:
        parse_pgn_text(pgn_text)
    except Exception as e:
        return jsonify({"error": f"PGN error: {e}"}), 400

    job_id = uuid.uuid4().hex
    ANALYSIS_JOBS[job_id] = {
        "status": "queued",
        "percent": 0,
        "done": 0,
        "total": 0,
        "message": "Starting Stockfish...",
        "result": None,
        "error": None,
        "created": time.time(),
    }

    thread = threading.Thread(
        target=run_analysis_job,
        args=(job_id, pgn_text, username, depth),
        daemon=True,
    )
    thread.start()
    return jsonify({"jobId": job_id})


@app.get("/api/analyze/status/<job_id>")
def analyze_status(job_id):
    job = ANALYSIS_JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Analysis job not found."}), 404

    payload = {
        "status": job["status"],
        "percent": job["percent"],
        "done": job["done"],
        "total": job["total"],
        "message": job["message"],
        "error": job["error"],
    }
    if job["status"] == "complete":
        payload["result"] = job["result"]
    return jsonify(payload)


@app.post("/api/analyze-position")
def analyze_position():
    global ACTIVE_ENGINE

    stockfish_path = find_stockfish()
    if not stockfish_path:
        return jsonify({"error": "Stockfish was not found. Configure it in Settings first."}), 400

    body = request.get_json(force=True)
    fen = (body.get("fen") or "").strip()
    depth = min(max(int(body.get("depth", 15)), 8), 22)
    multipv = min(max(int(body.get("multipv", 3)), 1), 5)

    try:
        board = chess.Board(fen)
    except Exception as e:
        return jsonify({"error": f"Invalid FEN: {e}"}), 400

    engine = None
    with ENGINE_LOCK:
        try:
            engine = chess.engine.SimpleEngine.popen_uci(stockfish_path)
            ACTIVE_ENGINE = engine
            infos = engine.analyse(board, chess.engine.Limit(depth=depth), multipv=multipv)
            if isinstance(infos, dict):
                infos = [infos]

            candidates = []
            for info in infos:
                pv = info.get("pv", [])
                first = pv[0] if pv else None
                pv_san = []
                pv_board = board.copy(stack=False)
                for pv_move in pv[:8]:
                    try:
                        pv_san.append(pv_board.san(pv_move))
                        pv_board.push(pv_move)
                    except Exception:
                        break

                candidates.append({
                    "moveSan": board.san(first) if first else "",
                    "moveUci": first.uci() if first else "",
                    "evalWhite": cp_to_eval(normalize_score(info["score"], chess.WHITE)),
                    "evalSideToMove": cp_to_eval(normalize_score(info["score"], board.turn)),
                    "pv": pv_san,
                })

            best = candidates[0] if candidates else None
            if best and best["evalSideToMove"] >= 150:
                explanation = "The side to move has a meaningful advantage. Focus on preserving pressure while limiting counterplay."
            elif best and best["evalSideToMove"] <= -150:
                explanation = "The side to move is under pressure. Look for defensive resources and ways to reduce the opponent's forcing options."
            else:
                explanation = "The position is relatively balanced. Compare candidate moves carefully rather than forcing the issue."

            best_teaching = None
            if best and best.get("moveUci"):
                try:
                    best_move = chess.Move.from_uci(best["moveUci"])
                    best_teaching = move_teaching(board, best_move)
                except Exception:
                    best_teaching = None

            return jsonify({
                "fen": board.fen(),
                "turn": "white" if board.turn else "black",
                "depth": depth,
                "candidates": candidates,
                "best": best,
                "coach": {
                    "explanation": explanation,
                    "lesson": "Scan checks, captures, and threats, then compare at least two candidate moves.",
                    "position": position_teaching_summary(board),
                    "bestMoveTeaching": best_teaching,
                },
            })
        except Exception as e:
            return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
        finally:
            if engine is not None:
                try:
                    engine.quit()
                except Exception:
                    try:
                        engine.close()
                    except Exception:
                        pass
            ACTIVE_ENGINE = None



@app.post("/api/explain-position")
def explain_position():
    body = request.get_json(force=True)
    fen = (body.get("fen") or "").strip()

    try:
        board = chess.Board(fen)
    except Exception as e:
        return jsonify({"error": f"Invalid FEN: {e}"}), 400

    summary = position_teaching_summary(board)

    checklist = [
        "What did the opponent's last move change?",
        "Is my king in danger?",
        "Is any piece attacked more times than it is defended?",
        "Do I have a useful check?",
        "Do I have a good capture?",
        "Can I create a threat?",
        "If nothing tactical is happening, which piece can I improve?",
    ]

    return jsonify({
        "turn": "white" if board.turn else "black",
        "summary": summary,
        "checklist": checklist,
    })

@app.post("/api/position/move")
def position_move():
    body = request.get_json(force=True)
    fen = body.get("fen", "")
    from_sq = body.get("from", "")
    to_sq = body.get("to", "")

    try:
        board = chess.Board(fen)
        base = from_sq + to_sq
        move = chess.Move.from_uci(base)

        # Auto-queen promotion when appropriate.
        piece = board.piece_at(chess.parse_square(from_sq))
        if piece and piece.piece_type == chess.PAWN and to_sq[1] in ("1", "8"):
            move = chess.Move.from_uci(base + "q")

        if move not in board.legal_moves:
            return jsonify({"error": "That move is not legal in this position."}), 400

        san = board.san(move)
        board.push(move)

        return jsonify({
            "fen": board.fen(),
            "san": san,
            "uci": move.uci(),
            "turn": "white" if board.turn else "black",
            "gameOver": board.is_game_over(),
        })
    except Exception as e:
        return jsonify({"error": f"Could not make that move: {e}"}), 400




@app.get("/api/learning/progress")
def learning_progress():
    return jsonify(progress_dashboard(load_learning()))


@app.post("/api/learning/feedback")
def learning_feedback():
    body = request.get_json(force=True)
    rating = (body.get("rating") or "").strip()
    concept = (body.get("concept") or "General").strip()
    source = (body.get("source") or "lesson").strip()

    allowed = {
        "I understand this",
        "Sort of",
        "I still don't get it",
    }

    if rating not in allowed:
        return jsonify({"error": "Invalid understanding rating."}), 400

    data = load_learning()
    data.setdefault("feedback", []).append({
        "rating": rating,
        "concept": concept,
        "source": source,
        "created": int(time.time()),
    })
    save_learning(data)

    return jsonify({"ok": True})


@app.get("/api/learning/profile")
def learning_profile():
    return jsonify(profile_from_learning(load_learning()))


@app.get("/api/learning/mistakes")
def learning_mistakes():
    data = load_learning()
    items = sorted(
        data.get("mistakes", []),
        key=lambda x: x.get("created", 0),
        reverse=True
    )
    return jsonify(items[:200])


@app.post("/api/learning/mistake/<mistake_id>/thought")
def save_mistake_thought(mistake_id):
    body = request.get_json(force=True)
    thought = (body.get("thought") or "").strip()
    tag = (body.get("tag") or "").strip()

    data = load_learning()
    found = None

    for item in data.get("mistakes", []):
        if item.get("id") == mistake_id:
            item["thought"] = thought
            item["thoughtTag"] = tag
            found = item
            break

    if not found:
        return jsonify({"error": "Mistake not found."}), 404

    save_learning(data)
    return jsonify(found)


@app.get("/api/training/next")
def training_next():
    data = load_learning()
    now = int(time.time())

    mistake_map = {
        item.get("id"): item
        for item in data.get("mistakes", [])
    }

    due = [
        t for t in data.get("training", [])
        if not t.get("retired")
        and t.get("nextDue", 0) <= now
        and t.get("mistakeId") in mistake_map
    ]

    if not due:
        return jsonify({"item": None})

    due.sort(
        key=lambda t: (
            t.get("nextDue", 0),
            t.get("box", 0),
            t.get("attempts", 0),
        )
    )

    training = due[0]
    mistake = mistake_map[training["mistakeId"]]

    return jsonify({
        "item": {
            "mistakeId": mistake["id"],
            "fen": mistake["fenBefore"],
            "color": mistake["color"],
            "moveNumber": mistake["moveNumber"],
            "classification": mistake["classification"],
            "category": mistake["category"],
            "playedSan": mistake["playedSan"],
            "bestSan": mistake["bestSan"],
            "bestUci": mistake["bestUci"],
            "coach": mistake.get("coach", {}),
            "training": training,
        }
    })


@app.post("/api/training/grade")
def training_grade():
    body = request.get_json(force=True)

    mistake_id = body.get("mistakeId")
    attempted_uci = (body.get("attemptedUci") or "").strip()
    correct = bool(body.get("correct"))

    data = load_learning()
    now = int(time.time())

    training = None
    for t in data.get("training", []):
        if t.get("mistakeId") == mistake_id:
            training = t
            break

    if not training:
        return jsonify({"error": "Training item not found."}), 404

    training["attempts"] = training.get("attempts", 0) + 1
    training["lastSeen"] = now

    if correct:
        training["correct"] = training.get("correct", 0) + 1
        training["box"] = min(training.get("box", 0) + 1, 4)
    else:
        training["box"] = max(training.get("box", 0) - 1, 0)

    if training["box"] >= 4 and training.get("correct", 0) >= 4:
        training["retired"] = True

    days = schedule_days(training["box"])
    training["nextDue"] = now + days * 86400

    save_learning(data)

    return jsonify({
        "ok": True,
        "box": training["box"],
        "nextDue": training["nextDue"],
        "retired": training["retired"],
    })


@app.get("/api/data/info")
def data_info():
    return jsonify({
        "dataDirectory": str(DATA_DIR),
        "configFile": str(CONFIG_FILE),
        "learningFile": str(LEARNING_FILE),
        "configExists": CONFIG_FILE.exists(),
        "learningExists": LEARNING_FILE.exists(),
    })


@app.get("/api/data/backup")
def data_backup():
    """
    Return all persistent Personal Chess Coach user data as JSON so the user
    can save a portable backup from the UI.
    """
    return jsonify({
        "version": 1,
        "exportedAt": int(time.time()),
        "config": load_config(),
        "learning": load_learning(),
    })


@app.post("/api/data/restore")
def data_restore():
    body = request.get_json(force=True)

    if not isinstance(body, dict):
        return jsonify({"error": "Invalid backup file."}), 400

    config = body.get("config")
    learning = body.get("learning")

    if config is not None and not isinstance(config, dict):
        return jsonify({"error": "Backup config is invalid."}), 400

    if learning is not None and not isinstance(learning, dict):
        return jsonify({"error": "Backup learning data is invalid."}), 400

    if config is not None:
        save_config(config)

    if learning is not None:
        save_learning(learning)

    return jsonify({"ok": True})



@app.post("/api/shutdown")
def shutdown():
    global ACTIVE_ENGINE

    if ACTIVE_ENGINE is not None:
        try:
            ACTIVE_ENGINE.quit()
        except Exception:
            try:
                ACTIVE_ENGINE.close()
            except Exception:
                pass
        ACTIVE_ENGINE = None

    shutdown_func = request.environ.get("werkzeug.server.shutdown")
    if shutdown_func:
        shutdown_func()
        return jsonify({"ok": True})

    def delayed_exit():
        time.sleep(0.35)
        os._exit(0)

    threading.Thread(target=delayed_exit, daemon=True).start()
    return jsonify({"ok": True})


if __name__ == "__main__":
    print()
    print("Personal Chess Coach v0.8")
    print("Open http://127.0.0.1:5000 in your browser.")
    print("Stockfish only runs while an analysis is active.")
    print(f"User data: {DATA_DIR}")
    print()
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
