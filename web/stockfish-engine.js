class StockfishEngine {
  constructor(options = {}) {
    this.workerUrl =
      options.workerUrl ||
      "./stockfish-18-lite-single.js";

    this.worker = null;

    this.ready = false;
    this.initializing = false;
    this.analyzing = false;

    this.currentResolve = null;
    this.currentReject = null;

    this.currentAnalysis = null;

    this.pendingReadyResolve = null;
    this.pendingReadyReject = null;

    this.onStatus =
      options.onStatus ||
      (() => {});

    this.onRawMessage =
      options.onRawMessage ||
      (() => {});
  }


  // =====================================================
  // START ENGINE
  // =====================================================

  async start() {
    if (this.ready && this.worker) {
      return;
    }

    if (this.initializing) {
      return new Promise(
        (resolve, reject) => {
          this.pendingReadyResolve = resolve;
          this.pendingReadyReject = reject;
        }
      );
    }

    this.initializing = true;

    this.onStatus(
      "Starting Stockfish..."
    );

    return new Promise(
      (resolve, reject) => {

        try {
          this.worker =
            new Worker(
              this.workerUrl
            );
        } catch (error) {
          this.initializing = false;

          reject(error);

          return;
        }


        // Store startup handlers.

        this.pendingReadyResolve =
          resolve;

        this.pendingReadyReject =
          reject;


        // -----------------------------------------------
        // WORKER MESSAGES
        // -----------------------------------------------

        this.worker.onmessage =
          (event) => {

            const message =
              String(
                event.data
              );

            this.onRawMessage(
              message
            );

            this.handleMessage(
              message
            );
          };


        // -----------------------------------------------
        // WORKER ERRORS
        // -----------------------------------------------

        this.worker.onerror =
          (error) => {

            this.onStatus(
              "Stockfish error."
            );

            this.initializing =
              false;

            this.ready =
              false;

            if (
              this.pendingReadyReject
            ) {
              this.pendingReadyReject(
                error
              );
            }

            if (
              this.currentReject
            ) {
              this.currentReject(
                error
              );
            }

            console.error(
              "Stockfish worker error:",
              error
            );
          };


        // Start UCI initialization.

        this.worker.postMessage(
          "uci"
        );
      }
    );
  }


  // =====================================================
  // HANDLE STOCKFISH OUTPUT
  // =====================================================

  handleMessage(message) {
    const trimmed =
      message.trim();


    // -----------------------------------------------
    // UCI initialized
    // -----------------------------------------------

    if (
      trimmed === "uciok"
    ) {
      this.worker.postMessage(
        "isready"
      );

      return;
    }


    // -----------------------------------------------
    // Engine fully ready
    // -----------------------------------------------

    if (
      trimmed === "readyok"
    ) {
      this.ready = true;

      this.initializing =
        false;

      this.onStatus(
        "Stockfish ready."
      );

      if (
        this.pendingReadyResolve
      ) {
        this.pendingReadyResolve();
      }

      this.pendingReadyResolve =
        null;

      this.pendingReadyReject =
        null;

      return;
    }


    // -----------------------------------------------
    // Stockfish analysis line
    // -----------------------------------------------

    if (
      trimmed.startsWith(
        "info "
      )
    ) {
      this.parseInfoLine(
        trimmed
      );

      return;
    }


    // -----------------------------------------------
    // Analysis finished
    // -----------------------------------------------

    if (
      trimmed.startsWith(
        "bestmove"
      )
    ) {
      this.finishAnalysis(
        trimmed
      );
    }
  }


  // =====================================================
  // PARSE "INFO" LINES
  // =====================================================

  parseInfoLine(line) {
    if (!this.currentAnalysis) {
      return;
    }

    const depthMatch =
      line.match(
        /\bdepth\s+(\d+)/
      );

    const scoreCpMatch =
      line.match(
        /\bscore\s+cp\s+(-?\d+)/
      );

    const scoreMateMatch =
      line.match(
        /\bscore\s+mate\s+(-?\d+)/
      );

    const nodesMatch =
      line.match(
        /\bnodes\s+(\d+)/
      );

    const npsMatch =
      line.match(
        /\bnps\s+(\d+)/
      );

    const timeMatch =
      line.match(
        /\btime\s+(\d+)/
      );

    const pvMatch =
      line.match(
        /\bpv\s+(.+)$/
      );


    if (depthMatch) {
      this.currentAnalysis.depth =
        Number(
          depthMatch[1]
        );
    }


    if (scoreCpMatch) {
      const cp =
        Number(
          scoreCpMatch[1]
        );

      this.currentAnalysis.score = {
        type: "cp",

        centipawns: cp,

        pawns:
          cp / 100
      };
    }


    if (scoreMateMatch) {
      const mate =
        Number(
          scoreMateMatch[1]
        );

      this.currentAnalysis.score = {
        type: "mate",

        mate
      };
    }


    if (nodesMatch) {
      this.currentAnalysis.nodes =
        Number(
          nodesMatch[1]
        );
    }


    if (npsMatch) {
      this.currentAnalysis.nps =
        Number(
          npsMatch[1]
        );
    }


    if (timeMatch) {
      this.currentAnalysis.timeMs =
        Number(
          timeMatch[1]
        );
    }


    if (pvMatch) {
      this.currentAnalysis.pv =
        pvMatch[1]
          .trim()
          .split(/\s+/);
    }
  }


  // =====================================================
  // FINISH ANALYSIS
  // =====================================================

  finishAnalysis(line) {
    if (
      !this.currentAnalysis
    ) {
      return;
    }

    const bestMoveMatch =
      line.match(
        /^bestmove\s+(\S+)/
      );

    const ponderMatch =
      line.match(
        /\bponder\s+(\S+)/
      );


    if (bestMoveMatch) {
      this.currentAnalysis.bestMove =
        bestMoveMatch[1];
    }


    if (ponderMatch) {
      this.currentAnalysis.ponder =
        ponderMatch[1];
    }


    const result = {
      ...this.currentAnalysis
    };


    this.analyzing =
      false;

    this.currentAnalysis =
      null;


    this.onStatus(
      "Analysis complete."
    );


    if (
      this.currentResolve
    ) {
      this.currentResolve(
        result
      );
    }


    this.currentResolve =
      null;

    this.currentReject =
      null;
  }


  // =====================================================
  // ANALYZE A FEN
  // =====================================================

  async analyzeFen(
    fen,
    depth = 15
  ) {
    if (!this.ready) {
      await this.start();
    }


    if (this.analyzing) {
      throw new Error(
        "Stockfish is already analyzing."
      );
    }


    this.analyzing = true;


    this.currentAnalysis = {
      fen,
      requestedDepth:
        depth,

      depth: 0,

      score: null,

      nodes: 0,

      nps: 0,

      timeMs: 0,

      pv: [],

      bestMove: null,

      ponder: null
    };


    this.onStatus(
      `Analyzing to depth ${depth}...`
    );


    return new Promise(
      (resolve, reject) => {

        this.currentResolve =
          resolve;

        this.currentReject =
          reject;


        this.worker.postMessage(
          "ucinewgame"
        );


        this.worker.postMessage(
          "isready"
        );


        this.worker.postMessage(
          `position fen ${fen}`
        );


        this.worker.postMessage(
          `go depth ${depth}`
        );
      }
    );
  }


  // =====================================================
  // ANALYZE STARTING POSITION
  // =====================================================

  async analyzeStartPosition(
    depth = 15
  ) {
    const startingFen =
      "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

    return this.analyzeFen(
      startingFen,
      depth
    );
  }


  // =====================================================
  // STOP CURRENT ANALYSIS
  // =====================================================

  stop() {
    if (
      !this.worker
    ) {
      return;
    }


    try {
      this.worker.postMessage(
        "stop"
      );
    } catch (error) {
      console.warn(
        "Could not stop Stockfish:",
        error
      );
    }
  }


  // =====================================================
  // COMPLETELY TERMINATE ENGINE
  // =====================================================

  destroy() {
    if (
      !this.worker
    ) {
      return;
    }


    try {
      this.worker.postMessage(
        "stop"
      );
    } catch {
      // Ignore.
    }


    this.worker.terminate();


    this.worker =
      null;

    this.ready =
      false;

    this.initializing =
      false;

    this.analyzing =
      false;

    this.currentAnalysis =
      null;

    this.currentResolve =
      null;

    this.currentReject =
      null;

    this.pendingReadyResolve =
      null;

    this.pendingReadyReject =
      null;


    this.onStatus(
      "Stockfish stopped."
    );
  }
}


// =======================================================
// GLOBAL EXPORT
// =======================================================
//
// We are not using a JavaScript build system yet.
//
// Attaching the class to window lets index.html create:
//
// const engine = new StockfishEngine();
//
// Later, when the web app gets more sophisticated,
// we can convert this into an ES module.
// =======================================================

window.StockfishEngine =
  StockfishEngine;
