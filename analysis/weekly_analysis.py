#!/usr/bin/env python3
"""
Chess Weekly Analysis
Récupère les parties Chess.com de la semaine, les analyse avec Stockfish,
génère du coaching via Gemini, sauvegarde JSON pour le site et envoie un email.
"""

import io
import json
import math
import os
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import chess
import chess.engine
import chess.pgn
import requests
from dotenv import load_dotenv

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
CHESS_USERNAME  = "Arata4526"
DATA_DIR        = Path(os.environ.get("DATA_DIR", "/home/alexis/Perso/Chess_Analysis/data"))
STOCKFISH_PATH  = os.environ.get("STOCKFISH_PATH", "/usr/games/stockfish")
ANALYSIS_DAYS   = 7

HEADERS = {
    "User-Agent": "ChessWeeklyAnalyzer/1.0 personal project",
    "Accept": "application/json",
}

STOCKFISH_LIMITS = {
    "bullet":      chess.engine.Limit(time=0.5),
    "blitz":       chess.engine.Limit(time=1.0),
    "rapid":       chess.engine.Limit(time=1.5),
    "daily":       chess.engine.Limit(depth=18),
}


# ─────────────────────────────────────────────
# Utilitaires
# ─────────────────────────────────────────────
def score_to_cp(score: chess.engine.PovScore) -> int:
    """Convertit un score Stockfish en centipions depuis le point de vue des blancs."""
    if score.is_mate():
        return 10000 if score.mate() > 0 else -10000
    return score.score()


def winrate(cp: int) -> float:
    """Probabilité de gain 0-100 (formule Lichess)."""
    cp = max(-1000, min(1000, cp))
    return 50 + 50 * (2 / (1 + math.exp(-0.004 * cp)) - 1)


def classify_move(eval_before_white: int, eval_after_white: int, mover_was_white: bool):
    """Classifie depuis la perspective du joueur qui vient de jouer.
    Retourne (classification, winrate_delta)."""
    sign = 1 if mover_was_white else -1
    delta = winrate(sign * eval_before_white) - winrate(sign * eval_after_white)
    if delta < 2:    return "best",       delta
    if delta < 5:    return "excellent",  delta
    if delta < 10:   return "good",       delta
    if delta < 20:   return "inaccuracy", delta
    if delta < 30:   return "mistake",    delta
    return "blunder", delta


def move_accuracy(winrate_delta: float) -> float:
    """Accuracy par coup 0-100 (formule Lichess CAPS)."""
    return max(0, min(100, 103.1668 * math.exp(-0.04354 * winrate_delta) - 3.1668))


def get_phase(half_move: int) -> str:
    move_num = half_move // 2 + 1
    if move_num <= 10:  return "opening"
    if move_num <= 30:  return "middlegame"
    return "endgame"


def extract_uuid(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


# ─────────────────────────────────────────────
# Chess.com API
# ─────────────────────────────────────────────
def fetch_month_games(username: str, year: int, month: int) -> list:
    url = f"https://api.chess.com/pub/player/{username}/games/{year}/{month:02d}"
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 404:
                return []
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 60))
                print(f"  Rate limited, attente {wait}s…")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json().get("games", [])
        except requests.RequestException as e:
            if attempt == 2:
                print(f"  Erreur API Chess.com : {e}")
                return []
            time.sleep(5 * (attempt + 1))
    return []


def fetch_recent_games(username: str, days: int = 7) -> list:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    months = {(now.year, now.month)}
    if (cutoff.year, cutoff.month) != (now.year, now.month):
        months.add((cutoff.year, cutoff.month))

    all_games = []
    for year, month in sorted(months):
        print(f"  Récupération Chess.com {year}/{month:02d}…")
        games = fetch_month_games(username, year, month)
        time.sleep(1)
        for g in games:
            end_time = datetime.fromtimestamp(g["end_time"], tz=timezone.utc)
            if end_time >= cutoff:
                all_games.append(g)

    return all_games


# ─────────────────────────────────────────────
# Analyse Stockfish
# ─────────────────────────────────────────────
def analyze_game(pgn_string: str, player_username: str,
                 engine: chess.engine.SimpleEngine, time_class: str) -> list:
    game = chess.pgn.read_game(io.StringIO(pgn_string))
    if game is None:
        raise ValueError("PGN invalide")

    board = game.board()
    white_player = game.headers.get("White", "").lower()
    player_color = chess.WHITE if white_player == player_username.lower() else chess.BLACK

    limit = STOCKFISH_LIMITS.get(time_class, STOCKFISH_LIMITS["blitz"])
    moves_list = list(game.mainline_moves())
    analyses = []

    for idx, move in enumerate(moves_list):
        fen_before = board.fen()

        # Évaluation AVANT le coup
        info_before = engine.analyse(board, limit)
        eval_before = score_to_cp(info_before["score"].white())
        best_move_obj = info_before.get("pv", [None])[0]

        san = board.san(move)
        uci = move.uci()
        board.push(move)
        fen_after = board.fen()

        # Évaluation APRÈS le coup
        info_after = engine.analyse(board, limit)
        eval_after = score_to_cp(info_after["score"].white())

        mover_was_white = (board.turn == chess.BLACK)
        if mover_was_white:
            cp_loss = max(0, eval_before - eval_after)
        else:
            cp_loss = max(0, eval_after - eval_before)

        classification, wr_delta = classify_move(eval_before, eval_after, mover_was_white)
        is_player_move = (board.turn != player_color)

        # Meilleur coup en SAN
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
            "winrate_delta":  round(wr_delta, 2),
            "classification": classification,
            "phase":          get_phase(idx),
            "is_player_move": is_player_move,
        })

    return analyses


# ─────────────────────────────────────────────
# Rapport par partie
# ─────────────────────────────────────────────
def build_game_report(raw_game: dict, analyses: list, username: str) -> dict:
    pgn_headers = {}
    game = chess.pgn.read_game(io.StringIO(raw_game.get("pgn", "")))
    if game:
        pgn_headers = dict(game.headers)

    white = raw_game.get("white", {})
    black = raw_game.get("black", {})
    player_color = "white" if white.get("username", "").lower() == username.lower() else "black"
    player_side  = white if player_color == "white" else black
    opp_side     = black if player_color == "white" else white

    result_raw = player_side.get("result", "")
    if result_raw in ("win",):
        result = "win"
    elif result_raw in ("checkmated", "resigned", "timeout", "abandoned"):
        result = "loss"
    else:
        result = "draw"

    # Stats du joueur uniquement
    player_moves = [m for m in analyses if m["is_player_move"]]
    blunders     = sum(1 for m in player_moves if m["classification"] == "blunder")
    mistakes     = sum(1 for m in player_moves if m["classification"] == "mistake")
    inaccuracies = sum(1 for m in player_moves if m["classification"] == "inaccuracy")

    # Précision par moyenne d'accuracy par coup (formule Lichess CAPS)
    accuracies = [move_accuracy(m.get("winrate_delta", 0)) for m in player_moves]
    accuracy = sum(accuracies) / len(accuracies) if accuracies else 0

    worst = max(player_moves, key=lambda m: m["cp_loss"], default=None)
    worst_move = {
        "half_move": worst["half_move"],
        "san":       worst["san"],
        "cp_loss":   worst["cp_loss"],
        "best_san":  worst["best_move_san"],
    } if worst and worst["cp_loss"] > 50 else None

    url = raw_game.get("url", "")
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
            "accuracy":    round(accuracy, 1),
            "blunders":    blunders,
            "mistakes":    mistakes,
            "inaccuracies": inaccuracies,
            "worst_move":  worst_move,
            "coaching":    "",   # rempli par Gemini
        },
        "moves": analyses,
    }


# ─────────────────────────────────────────────
# Statistiques hebdo
# ─────────────────────────────────────────────
def aggregate_stats(reports: list) -> dict:
    by_class = {}
    total_accuracy = 0
    errors_by_phase = {"opening": {"blunders": 0, "mistakes": 0, "inaccuracies": 0},
                       "middlegame": {"blunders": 0, "mistakes": 0, "inaccuracies": 0},
                       "endgame":   {"blunders": 0, "mistakes": 0, "inaccuracies": 0}}
    openings_white, openings_black = {}, {}

    for r in reports:
        tc = r["meta"]["time_class"]
        if tc not in by_class:
            by_class[tc] = {"played": 0, "wins": 0, "losses": 0, "draws": 0}
        by_class[tc]["played"] += 1
        by_class[tc][r["meta"]["result"] + "s"] = by_class[tc].get(r["meta"]["result"] + "s", 0) + 1

        total_accuracy += r["summary"]["accuracy"]

        player_moves = [m for m in r["moves"] if m["is_player_move"]]
        for m in player_moves:
            phase = m["phase"]
            if m["classification"] == "blunder":    errors_by_phase[phase]["blunders"] += 1
            elif m["classification"] == "mistake":  errors_by_phase[phase]["mistakes"] += 1
            elif m["classification"] == "inaccuracy": errors_by_phase[phase]["inaccuracies"] += 1

        opening = r["meta"].get("opening", "")
        if opening:
            if r["meta"]["player_color"] == "white":
                openings_white[opening] = openings_white.get(opening, 0) + 1
            else:
                openings_black[opening] = openings_black.get(opening, 0) + 1

    return {
        "total_games":     len(reports),
        "by_time_class":   by_class,
        "avg_accuracy":    round(total_accuracy / max(len(reports), 1), 1),
        "errors_by_phase": errors_by_phase,
        "openings":        {
            "as_white": dict(sorted(openings_white.items(), key=lambda x: -x[1])[:5]),
            "as_black":  dict(sorted(openings_black.items(),  key=lambda x: -x[1])[:5]),
        },
    }


def get_top_blunders(reports: list, n: int = 3) -> list:
    blunders = []
    for r in reports:
        for m in r["moves"]:
            if m["is_player_move"] and m["classification"] in ("blunder", "mistake"):
                opp = r["meta"]["black"]["username"] if r["meta"]["player_color"] == "white" \
                      else r["meta"]["white"]["username"]
                blunders.append({
                    "opponent":    opp,
                    "time_class":  r["meta"]["time_class"],
                    "move_number": m["move_number"],
                    "san":         m["san"],
                    "cp_loss":     m["cp_loss"],
                    "best_san":    m["best_move_san"],
                    "phase":       m["phase"],
                    "game_uuid":   r["meta"]["uuid"],
                })
    return sorted(blunders, key=lambda x: -x["cp_loss"])[:n]


# ─────────────────────────────────────────────
# Gemini coaching
# ─────────────────────────────────────────────
def generate_game_coaching(report: dict) -> str:
    try:
        import google.generativeai as genai
        genai.configure(api_key=os.environ["GEMINI_API_KEY"])
        model = genai.GenerativeModel("gemini-1.5-flash")

        meta    = report["meta"]
        summary = report["summary"]
        opp     = meta["black"]["username"] if meta["player_color"] == "white" \
                  else meta["white"]["username"]

        worst = summary.get("worst_move")
        worst_text = f"Pire coup : {worst['san']} (perte de {worst['cp_loss']} cp, meilleur : {worst['best_san']})" \
                     if worst else "Aucune gaffe majeure"

        prompt = f"""Tu es un coach d'échecs expert et bienveillant. Donne un conseil court (3-4 phrases max)
en français sur cette partie d'échecs, en t'adressant directement au joueur.

Partie : {meta['player_color'].upper()} vs {opp} ({meta['time_class']}) — {meta['result'].upper()}
Ouverture : {meta.get('opening', '?')}
Précision : {summary['accuracy']}%
Erreurs : {summary['blunders']} gaffes (??) · {summary['mistakes']} erreurs (?) · {summary['inaccuracies']} imprécisions (?!)
{worst_text}

Sois direct, constructif, et donne un conseil actionnable basé sur ces données."""

        resp = model.generate_content(prompt)
        return resp.text.strip()
    except Exception as e:
        print(f"  Coaching par partie échoué : {e}")
        return ""


def generate_weekly_coaching(stats: dict, top_blunders: list) -> str:
    try:
        import google.generativeai as genai
        genai.configure(api_key=os.environ["GEMINI_API_KEY"])
        model = genai.GenerativeModel("gemini-1.5-flash")

        blunder_text = "\n".join(
            f"{i+1}. Coup {b['move_number']} : {b['san']} (perte {b['cp_loss']}cp, "
            f"meilleur : {b['best_san']}, phase : {b['phase']})"
            for i, b in enumerate(top_blunders)
        )

        prompt = f"""Tu es un coach d'échecs expert. Analyse ces statistiques hebdomadaires du joueur {CHESS_USERNAME}
et génère un rapport de coaching en français avec exactement ce format markdown :

## Résumé de la semaine
[2-3 phrases de bilan, ton encourageant]

## 3 axes d'amélioration prioritaires
1. [Conseil concret avec explication]
2. [Conseil concret avec explication]
3. [Conseil concret avec explication]

## Conseils d'ouverture
[Recommandations basées sur les ouvertures jouées]

## Thèmes tactiques à travailler
[2-3 thèmes tactiques identifiés dans les erreurs]

---
STATS :
- Parties : {stats['total_games']} | Précision moyenne : {stats['avg_accuracy']}%
- Par format : {json.dumps(stats['by_time_class'], ensure_ascii=False)}
- Erreurs par phase : {json.dumps(stats['errors_by_phase'], ensure_ascii=False)}
- Ouvertures blancs : {list(stats['openings']['as_white'].keys())[:3]}
- Ouvertures noirs : {list(stats['openings']['as_black'].keys())[:3]}

TOP ERREURS :
{blunder_text or 'Aucune gaffe majeure cette semaine !'}"""

        resp = model.generate_content(prompt)
        return resp.text.strip()
    except Exception as e:
        print(f"  Coaching hebdo échoué : {e}")
        return f"Coaching indisponible ({e}). Précision moyenne : {stats['avg_accuracy']}%."


# ─────────────────────────────────────────────
# Sauvegarde JSON
# ─────────────────────────────────────────────
def save_game_json(report: dict) -> None:
    path = DATA_DIR / f"{report['meta']['uuid']}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


def save_latest_json(reports: list, stats: dict, coaching: str) -> None:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)

    games_index = []
    for r in reports:
        meta    = r["meta"]
        summary = r["summary"]
        opp = meta["black"]["username"] if meta["player_color"] == "white" \
              else meta["white"]["username"]
        opp_rating = meta["black"]["rating"] if meta["player_color"] == "white" \
                     else meta["white"]["rating"]
        games_index.append({
            "uuid":            meta["uuid"],
            "date":            meta["date"],
            "time_class":      meta["time_class"],
            "result":          meta["result"],
            "opponent":        opp,
            "opponent_rating": opp_rating,
            "opening":         meta.get("opening", ""),
            "accuracy":        summary["accuracy"],
            "blunders":        summary["blunders"],
            "mistakes":        summary["mistakes"],
            "inaccuracies":    summary["inaccuracies"],
        })

    payload = {
        "generated_at": now.isoformat(),
        "week_start":   cutoff.strftime("%Y-%m-%d"),
        "week_end":     now.strftime("%Y-%m-%d"),
        "player":       CHESS_USERNAME,
        "stats":        stats,
        "coaching":     coaching,
        "games":        sorted(games_index, key=lambda x: x["date"], reverse=True),
    }

    with open(DATA_DIR / "latest.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────────
# Email
# ─────────────────────────────────────────────
def build_email_html(stats: dict, coaching: str, reports: list) -> str:
    site_url = os.environ.get("SITE_URL", "http://localhost:8765")

    def md_to_html(text):
        lines, out = text.split("\n"), []
        for line in lines:
            if line.startswith("## "):  out.append(f"<h3>{line[3:]}</h3>")
            elif line.startswith("### "): out.append(f"<h4>{line[4:]}</h4>")
            elif line.strip().startswith(("1.", "2.", "3.", "4.")):
                out.append(f"<li>{line.strip()[2:].strip()}</li>")
            elif line.strip():
                out.append(f"<p>{line}</p>")
        return "\n".join(out)

    rows = ""
    for r in sorted(reports, key=lambda x: x["meta"]["end_time"], reverse=True):
        meta = r["meta"]
        s = r["summary"]
        opp = meta["black"]["username"] if meta["player_color"] == "white" \
              else meta["white"]["username"]
        color = {"win": "#81b64c", "loss": "#f44336", "draw": "#9e9e9e"}[meta["result"]]
        label = {"win": "V", "loss": "D", "draw": "N"}[meta["result"]]
        worst = s.get("worst_move")
        worst_txt = f"{worst['san']} ({worst['cp_loss']}cp)" if worst else "–"
        rows += f"""
        <tr>
          <td style="text-align:center">
            <span style="background:{color}22;color:{color};padding:2px 8px;border-radius:12px;font-weight:700">{label}</span>
          </td>
          <td><strong>vs {opp}</strong><br><small style="color:#9e9e9e">{meta['opening'] or '–'}</small></td>
          <td style="text-align:center">{meta['time_class']}</td>
          <td style="text-align:center;color:#81b64c;font-weight:700">{s['accuracy']}%</td>
          <td style="text-align:center;color:#f44336">{worst_txt}</td>
          <td style="text-align:center"><a href="{site_url}/game.html?id={meta['uuid']}" style="color:#81b64c">Voir →</a></td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8">
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#1e1e1e;color:#e0e0e0;max-width:720px;margin:0 auto;padding:20px}}
  h1{{background:#272727;color:#e0e0e0;padding:20px 24px;border-radius:10px;font-size:20px;border-left:4px solid #81b64c;margin-bottom:20px}}
  h3{{color:#81b64c;font-size:14px;text-transform:uppercase;letter-spacing:.5px;margin:16px 0 6px}}
  h4{{color:#e0e0e0;font-size:13px;margin:10px 0 4px}}
  p,li{{color:#9e9e9e;line-height:1.65;font-size:13px}}
  li{{margin-bottom:4px}}
  .stats{{display:flex;gap:12px;margin:16px 0;flex-wrap:wrap}}
  .stat{{background:#272727;border:1px solid #3d3d3d;border-radius:8px;padding:12px 16px;text-align:center;min-width:80px}}
  .stat .v{{font-size:24px;font-weight:700}}
  .stat .l{{font-size:11px;color:#9e9e9e;text-transform:uppercase}}
  .coaching{{background:#272727;border:1px solid #3d3d3d;border-left:3px solid #81b64c;border-radius:8px;padding:16px 20px;margin:16px 0}}
  table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}}
  th{{background:#272727;color:#9e9e9e;padding:8px;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.4px;border-bottom:1px solid #3d3d3d}}
  td{{padding:10px 8px;border-bottom:1px solid #2c2c2c;vertical-align:middle}}
  a{{color:#81b64c}}
  .footer{{text-align:center;color:#555;font-size:11px;margin-top:24px}}
</style>
</head>
<body>
<h1>♟ Chess Analysis · Semaine du {stats.get('week_start','')}</h1>

<div class="stats">
  <div class="stat"><div class="v">{stats['total_games']}</div><div class="l">Parties</div></div>
  <div class="stat"><div class="v" style="color:#81b64c">{stats['avg_accuracy']}%</div><div class="l">Précision</div></div>
  {''.join(f'<div class="stat"><div class="v">{v["played"]}</div><div class="l">{k}</div></div>' for k, v in stats.get('by_time_class', {}).items())}
</div>

<div class="coaching">
  <div style="font-size:12px;font-weight:600;color:#81b64c;text-transform:uppercase;letter-spacing:.5px;margin-bottom:10px">🤖 Coaching IA</div>
  {md_to_html(coaching)}
</div>

<h3>Parties de la semaine</h3>
<table>
  <tr>
    <th>Résultat</th><th>Adversaire</th><th>Format</th><th>Précision</th><th>Pire coup</th><th>Lien</th>
  </tr>
  {rows}
</table>

<div class="footer">
  Généré le {datetime.now(timezone.utc).strftime('%d/%m/%Y à %H:%M')} UTC ·
  <a href="{site_url}">Voir le site complet</a>
</div>
</body></html>"""


def send_email(stats: dict, coaching: str, reports: list) -> None:
    gmail_user = os.environ.get("GMAIL_USER", "")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
    recipient  = os.environ.get("RECIPIENT_EMAIL", gmail_user)

    if not gmail_user or not gmail_pass:
        print("  Email ignoré (GMAIL_USER / GMAIL_APP_PASSWORD non configurés)")
        return

    html = build_email_html(stats, coaching, reports)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"♟ Chess | Semaine du {stats.get('week_start','')} · {stats['total_games']} parties · {stats['avg_accuracy']}% précision"
    msg["From"]    = gmail_user
    msg["To"]      = recipient

    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(gmail_user, gmail_pass)
        server.send_message(msg)
    print(f"  Email envoyé à {recipient}")


# ─────────────────────────────────────────────
# Nettoyage ancien JSON (> 12 semaines)
# ─────────────────────────────────────────────
def cleanup_old_data(keep_days: int = 20) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    for f in DATA_DIR.glob("*.json"):
        if f.name == "latest.json":
            continue
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            f.unlink()
            print(f"  Supprimé : {f.name}")


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
def main():
    load_dotenv(Path(__file__).parent / ".env")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Démarrage analyse Chess.com pour {CHESS_USERNAME}")

    # 1. Récupération des parties
    print("→ Récupération des parties…")
    raw_games = fetch_recent_games(CHESS_USERNAME, days=ANALYSIS_DAYS)
    print(f"  {len(raw_games)} partie(s) trouvée(s) sur les {ANALYSIS_DAYS} derniers jours")

    if not raw_games:
        print("  Aucune partie, arrêt.")
        return

    # 2. Analyse Stockfish
    print("→ Initialisation Stockfish…")
    try:
        engine = chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH)
        engine.configure({"Threads": 2, "Hash": 128})
    except FileNotFoundError:
        print(f"  ERREUR : Stockfish introuvable à {STOCKFISH_PATH}")
        sys.exit(1)

    reports = []
    try:
        for i, raw_game in enumerate(raw_games):
            uuid = extract_uuid(raw_game.get("url", ""))
            print(f"→ Partie {i+1}/{len(raw_games)} ({uuid}) [{raw_game.get('time_class','')}]…")
            try:
                analyses = analyze_game(
                    raw_game["pgn"], CHESS_USERNAME, engine,
                    raw_game.get("time_class", "blitz")
                )
                report = build_game_report(raw_game, analyses, CHESS_USERNAME)
                reports.append(report)
                save_game_json(report)
                print(f"  ✓ {report['summary']['blunders']}?? {report['summary']['mistakes']}? — {report['summary']['accuracy']}%")
            except Exception as e:
                print(f"  ✗ Erreur : {e}")
                continue
    finally:
        engine.quit()

    if not reports:
        print("Aucune partie analysée avec succès.")
        return

    # 3. Coaching Gemini par partie
    if os.environ.get("GEMINI_API_KEY"):
        print("→ Coaching IA par partie…")
        for r in reports:
            r["summary"]["coaching"] = generate_game_coaching(r)
            save_game_json(r)   # mise à jour avec coaching
            time.sleep(1)       # éviter rate limit Gemini
    else:
        print("  GEMINI_API_KEY manquant, coaching ignoré")

    # 4. Statistiques hebdo + coaching global
    print("→ Agrégation des stats…")
    stats = aggregate_stats(reports)
    stats["week_start"] = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

    print("→ Coaching hebdomadaire…")
    top_bl = get_top_blunders(reports)
    weekly_coaching = generate_weekly_coaching(stats, top_bl) if os.environ.get("GEMINI_API_KEY") else ""

    # 5. Sauvegarde latest.json
    print("→ Sauvegarde latest.json…")
    save_latest_json(reports, stats, weekly_coaching)

    # 6. Email
    print("→ Envoi email…")
    send_email(stats, weekly_coaching, reports)

    # 7. Nettoyage
    cleanup_old_data()

    print(f"✓ Terminé. {len(reports)} partie(s) analysée(s).")


if __name__ == "__main__":
    main()
