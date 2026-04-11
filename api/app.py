#!/usr/bin/env python3
"""
Chess Analysis API
- GET  /api/games/<year>/<month>   proxy Chess.com API
- POST /api/analyze                déclenche l'analyse d'une partie
- GET  /api/status/<uuid>          statut de l'analyse
"""

import io
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import chess
import chess.engine
import chess.pgn
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS

load_dotenv("/data/.env")

app = Flask(__name__)
# CORS limité à l'origine du site uniquement
CORS(app)


CHESS_USERNAME = "Arata4526"
DATA_DIR       = Path(os.environ.get("DATA_DIR", "/data"))
STOCKFISH_PATH = os.environ.get("STOCKFISH_PATH", "/usr/games/stockfish")

HEADERS = {
    "User-Agent": "ChessWeeklyAnalyzer/1.0 personal project",
    "Accept": "application/json",
}

STOCKFISH_LIMITS = {
    "bullet": chess.engine.Limit(time=0.10),
    "blitz":  chess.engine.Limit(time=0.15),
    "rapid":  chess.engine.Limit(time=0.30),
    "daily":  chess.engine.Limit(depth=15),
}

# statut des analyses en cours : uuid -> "pending"|"done"|"error"
analysis_jobs: dict[str, str] = {}


# ─── Helpers ────────────────────────────────────────────────────────────────

def score_to_cp(score):
    if score.is_mate():
        return 10000 if score.mate() > 0 else -10000
    return score.score()

def classify_move(cp_loss):
    if cp_loss <= 10:   return "best"
    if cp_loss <= 25:   return "excellent"
    if cp_loss <= 50:   return "good"
    if cp_loss <= 100:  return "inaccuracy"
    if cp_loss <= 200:  return "mistake"
    return "blunder"

def get_phase(half_move):
    n = half_move // 2 + 1
    if n <= 10:  return "opening"
    if n <= 30:  return "middlegame"
    return "endgame"

def extract_uuid(url):
    return url.rstrip("/").split("/")[-1]


# ─── Analyse Stockfish ───────────────────────────────────────────────────────

def analyze_game(pgn_string, player_username, engine, time_class):
    game = chess.pgn.read_game(io.StringIO(pgn_string))
    if game is None:
        raise ValueError("PGN invalide")

    board = game.board()
    white = game.headers.get("White", "").lower()
    player_color = chess.WHITE if white == player_username.lower() else chess.BLACK
    limit = STOCKFISH_LIMITS.get(time_class, STOCKFISH_LIMITS["blitz"])
    analyses = []

    for idx, move in enumerate(game.mainline_moves()):
        fen_before = board.fen()
        info_before = engine.analyse(board, limit)
        eval_before = score_to_cp(info_before["score"].white())
        best_move_obj = info_before.get("pv", [None])[0]

        san = board.san(move)
        uci = move.uci()
        board.push(move)
        fen_after = board.fen()

        info_after = engine.analyse(board, limit)
        eval_after = score_to_cp(info_after["score"].white())

        if board.turn == chess.WHITE:
            cp_loss = max(0, eval_after - eval_before)
        else:
            cp_loss = max(0, eval_before - eval_after)

        is_player_move = (board.turn != player_color)

        best_san = san
        if best_move_obj and best_move_obj != move:
            board.pop()
            try:
                best_san = board.san(best_move_obj)
            except Exception:
                best_san = best_move_obj.uci()
            board.push(move)

        analyses.append({
            "half_move":      idx + 1,
            "move_number":    idx // 2 + 1,
            "color":          "white" if idx % 2 == 0 else "black",
            "san":            san,
            "uci":            uci,
            "fen_before":     fen_before,
            "fen_after":      fen_after,
            "eval_before":    eval_before,
            "eval_after":     eval_after,
            "best_move_san":  best_san,
            "best_move_uci":  best_move_obj.uci() if best_move_obj else uci,
            "cp_loss":        cp_loss,
            "classification": classify_move(cp_loss) if is_player_move else "n/a",
            "phase":          get_phase(idx),
            "is_player_move": is_player_move,
        })

    return analyses


def build_game_report(raw_game, analyses, username):
    pgn_headers = {}
    game = chess.pgn.read_game(io.StringIO(raw_game.get("pgn", "")))
    if game:
        pgn_headers = dict(game.headers)

    white = raw_game.get("white", {})
    black = raw_game.get("black", {})
    player_color = "white" if white.get("username", "").lower() == username.lower() else "black"
    player_side  = white if player_color == "white" else black

    result_raw = player_side.get("result", "")
    if result_raw == "win":
        result = "win"
    elif result_raw in ("checkmated", "resigned", "timeout", "abandoned"):
        result = "loss"
    else:
        result = "draw"

    player_moves = [m for m in analyses if m["is_player_move"]]
    blunders     = sum(1 for m in player_moves if m["classification"] == "blunder")
    mistakes     = sum(1 for m in player_moves if m["classification"] == "mistake")
    inaccuracies = sum(1 for m in player_moves if m["classification"] == "inaccuracy")

    total_cp = sum(m["cp_loss"] for m in player_moves)
    accuracy = max(0, 100 - (total_cp / max(len(player_moves), 1) / 10))

    worst = max(player_moves, key=lambda m: m["cp_loss"], default=None)
    worst_move = {
        "half_move": worst["half_move"],
        "san":       worst["san"],
        "cp_loss":   worst["cp_loss"],
        "best_san":  worst["best_move_san"],
    } if worst and worst["cp_loss"] > 50 else None

    url      = raw_game.get("url", "")
    end_time = raw_game.get("end_time", 0)
    date_str = datetime.fromtimestamp(end_time, tz=timezone.utc).strftime("%Y-%m-%d")

    return {
        "meta": {
            "uuid":         extract_uuid(url),
            "url":          url,
            "date":         date_str,
            "end_time":     end_time,
            "time_class":   raw_game.get("time_class", ""),
            "time_control": raw_game.get("time_control", ""),
            "opening":      pgn_headers.get("ECOUrl", "").split("/")[-1].replace("-", " ").title()
                            or pgn_headers.get("Opening", ""),
            "result":       result,
            "player_color": player_color,
            "white":        {"username": white.get("username", ""), "rating": white.get("rating", 0)},
            "black":        {"username": black.get("username", ""), "rating": black.get("rating", 0)},
            "termination":  pgn_headers.get("Termination", ""),
        },
        "summary": {
            "accuracy":     round(accuracy, 1),
            "blunders":     blunders,
            "mistakes":     mistakes,
            "inaccuracies": inaccuracies,
            "worst_move":   worst_move,
            "coaching":     "",
        },
        "moves": analyses,
    }


def generate_coaching(report):
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return ""
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-1.5-flash")

        meta    = report["meta"]
        summary = report["summary"]
        opp     = meta["black"]["username"] if meta["player_color"] == "white" \
                  else meta["white"]["username"]
        worst   = summary.get("worst_move")
        worst_t = f"Pire coup : {worst['san']} (perte {worst['cp_loss']}cp, meilleur : {worst['best_san']})" \
                  if worst else "Aucune gaffe majeure"

        resp = model.generate_content(
            f"Tu es un coach d'échecs expert. Donne un conseil court (3-4 phrases) en français "
            f"sur cette partie, en t'adressant directement au joueur.\n\n"
            f"Partie : {meta['player_color'].upper()} vs {opp} ({meta['time_class']}) — {meta['result'].upper()}\n"
            f"Ouverture : {meta.get('opening','?')}\n"
            f"Précision : {summary['accuracy']}%\n"
            f"Erreurs : {summary['blunders']}?? · {summary['mistakes']}? · {summary['inaccuracies']}?!\n"
            f"{worst_t}\n\nSois direct, constructif, et donne un conseil actionnable."
        )
        return resp.text.strip()
    except Exception as e:
        print(f"Gemini error: {e}")
        return ""


def run_analysis_job(uuid, raw_game):
    """Tourne dans un thread séparé."""
    analysis_jobs[uuid] = "pending"
    try:
        engine = chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH)
        engine.configure({"Threads": 2, "Hash": 128})
        try:
            analyses = analyze_game(
                raw_game["pgn"], CHESS_USERNAME, engine,
                raw_game.get("time_class", "blitz")
            )
            report = build_game_report(raw_game, analyses, CHESS_USERNAME)
        finally:
            engine.quit()

        report["summary"]["coaching"] = generate_coaching(report)

        path = DATA_DIR / f"{uuid}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        analysis_jobs[uuid] = "done"
    except Exception as e:
        print(f"Analysis failed for {uuid}: {e}")
        analysis_jobs[uuid] = "error"


# ─── Routes ─────────────────────────────────────────────────────────────────

@app.route("/api/games/<int:year>/<int:month>")
def get_games(year, month):
    """Proxy Chess.com API et indique quelles parties sont déjà analysées."""
    url = f"https://api.chess.com/pub/player/{CHESS_USERNAME}/games/{year}/{month:02d}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        games = r.json().get("games", [])
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    # Marquer les parties déjà analysées
    for g in games:
        uid = extract_uuid(g.get("url", ""))
        g["uuid"]     = uid
        g["analyzed"] = (DATA_DIR / f"{uid}.json").exists()

    return jsonify({"games": games})


@app.route("/api/analyze", methods=["POST"])
def start_analyze():
    """Lance l'analyse d'une partie en background."""
    data = request.get_json()
    if not data or "uuid" not in data:
        return jsonify({"error": "uuid requis"}), 400

    uuid = data["uuid"]

    # Déjà analysé
    if (DATA_DIR / f"{uuid}.json").exists():
        return jsonify({"status": "done"})

    # Déjà en cours
    if analysis_jobs.get(uuid) == "pending":
        return jsonify({"status": "pending"})

    # Lancer en background
    t = threading.Thread(target=run_analysis_job, args=(uuid, data), daemon=True)
    t.start()

    return jsonify({"status": "started"})


@app.route("/api/status/<uuid>")
def get_status(uuid):
    """Retourne le statut d'une analyse."""
    if (DATA_DIR / f"{uuid}.json").exists():
        return jsonify({"status": "done"})
    status = analysis_jobs.get(uuid, "not_started")
    return jsonify({"status": status})


@app.route("/api/archives")
def get_archives():
    """Liste les archives mensuelles disponibles pour le joueur."""
    url = f"https://api.chess.com/pub/player/{CHESS_USERNAME}/games/archives"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        archives = r.json().get("archives", [])
        # Extraire YYYY/MM depuis les URLs
        months = [a.split("/")[-2] + "/" + a.split("/")[-1] for a in archives]
        return jsonify({"archives": sorted(months, reverse=True)})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=5000, threaded=True)
