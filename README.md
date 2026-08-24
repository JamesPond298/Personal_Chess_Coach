# Personal Chess Coach v0.8

Local-first chess postgame review and position study.

## Start on Windows

1. Extract the folder somewhere local, such as `C:\Chess\personal_chess_coach_v0.4`.
2. Double-click `START_COACH.bat`.
3. In Settings, select your working Stockfish `.exe`.
4. Keep the command window open while using the app.
5. Use **Shut Down Coach** when finished.

## v0.4 changes

- Strongly distinct white and black SVG pieces.
- Real postgame analysis progress bar based on completed reviewed moves.
- One-click **Study position** transfer from reviewed moves to Position Lab.
- Position Lab board supports click-to-move and drag-and-drop legal moves.
- Undo, flip, starting-position and FEN controls in Position Lab.
- Clear **YOU PLAYED** vs **STOCKFISH PREFERRED** comparison cards.
- Full report overlay and downloadable HTML report retained.
- Stockfish is started only during active analysis and shut down afterward.

This is a study and postgame-review tool. It is not designed to connect to or assist with active online games.


## v0.4.1 Position Lab changes

- Candidate moves are now clickable with **Play move**.
- Playing a candidate updates the board immediately.
- **Back** steps backward one move through the line you are exploring.
- **Reset Branch** returns to the original imported/reviewed/FEN choice point.
- Loading a FEN, starting a new board, or importing a reviewed position establishes a new branch base.


## v0.5 Learning System

- **Coach Challenge**: retry positions from your own mistakes before seeing Stockfish's answer.
- **Train My Mistakes**: automatically turns inaccuracies, mistakes, and blunders into training positions.
- **Spaced repetition**: correctly solved positions move through longer review intervals.
- **Mistake Journal**: persistent history of your recurring errors across reviewed games.
- **Chess Profile**: tracks recurring weakness categories and how many training positions are due.
- **Thought reflections**: save what you were thinking, including calculation errors, time pressure, missed replies, and other causes.
- **Study from Journal**: send an old mistake directly back into Position Lab.

The learning data is stored locally in `data/learning.json`.


## v0.6 Beginner Teaching Layer

- Explains **why** Stockfish prefers a move.
- Explains **what the move accomplishes**.
- Gives a **next plan** instead of stopping at one engine move.
- Labels the underlying concept: development, king safety, center control, forcing move, capture/material, or piece improvement.
- Adds a permanent beginner **Before Every Move** checklist.
- Adds **Explain This Position** in Position Lab with material, development, center, king safety, loose-piece checks, and guided questions.
- Explains Stockfish principal-variation moves in plain English.
- Continues to use your own reviewed mistakes for training and spaced repetition.

## Future public/web direction

The current app is deliberately local-first. A future hosted version can keep the same UI and learning model while replacing the local Flask/Stockfish process with a hosted analysis service and user accounts.

Because Stockfish is GPLv3, any distributed or hosted product should be designed carefully around license obligations. The coach's own proprietary teaching, account, analytics, and subscription layers can be architected separately from the Stockfish engine component.

Possible future paid features could include cloud history sync, friend groups, richer AI explanations, deeper analysis, shared study rooms, coach dashboards, and personalized training plans.


## v0.7 Measurement and Progress

- New **Progress Dashboard**.
- Establishes the first 10 reviewed games as your initial baseline.
- Compares baseline performance with your most recent 10 games.
- Tracks blunders, mistakes, inaccuracies, and estimated accuracy per game.
- Tracks training accuracy and repeat-position retention.
- Adds concept mastery scores based on actual exercise performance and spaced-repetition progress.
- Adds **I understand this / Sort of / I still don't get it** feedback to lessons.
- Uses training data to identify a current learning priority.
- Adds explicit 10-, 25-, 50-, and 100-game checkpoints.
- Keeps all measurement data local in `data/learning.json`.

### Important interpretation

The dashboard is intended to show trends, not judge individual games. Chess results and engine accuracy are noisy. Improvement is best indicated by fewer basic mistakes, better retention, stronger recognition of recurring concepts, and eventually transfer to unfamiliar positions.


## v0.8 Permanent Data Storage

Starting with v0.8, your learning database is no longer stored inside each
version's application folder.

On Windows it is stored at:

`%LOCALAPPDATA%\PersonalChessCoach`

Usually this resolves to something like:

`C:\Users\<your-name>\AppData\Local\PersonalChessCoach`

This directory contains:

- `config.json` — Stockfish configuration.
- `learning.json` — reviewed games, mistakes, training history, reflections,
  mastery, retention, and progress data.

### Automatic migration

When v0.8 starts, it checks its own old-style `data` folder. If `config.json`
or `learning.json` exist there and the permanent versions do not yet exist,
they are copied automatically.

After the first migration, future Personal Chess Coach versions can be extracted
to completely new folders without copying the learning database.

### Backup and restore

Settings now includes:

- **Download Backup** — exports your config and learning history to a JSON file.
- **Restore Backup** — imports one of those backup files.

This is useful even after cloud sync is introduced later.
