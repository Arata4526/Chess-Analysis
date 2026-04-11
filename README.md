# Chess Analysis

Système d'analyse hebdomadaire des parties Chess.com pour Arata4526.

## Stack
- **Python + Stockfish** — analyse coup par coup
- **Google Gemini** — coaching IA personnalisé
- **nginx (Docker)** — site interactif style Chess.com
- **Cloudflare Tunnel** — accès externe via `chess.alexisbousquet.fr`

## Structure
```
Chess_Analysis/
├── docker-compose.yml      # nginx container
├── nginx/nginx.conf
├── web/                    # site statique (index + game viewer)
├── data/                   # JSON générés par le script (gitignored)
└── analysis/
    ├── weekly_analysis.py  # script principal
    ├── requirements.txt
    └── .env                # clés API (gitignored)
```

## Setup Pi

### 1. Dépendances
```bash
sudo apt install -y stockfish python3-venv
cd analysis && python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configuration
```bash
cp analysis/.env.example analysis/.env
nano analysis/.env   # remplir les clés
```

**Clés nécessaires :**
- `GEMINI_API_KEY` → [aistudio.google.com](https://aistudio.google.com)
- `GMAIL_APP_PASSWORD` → Google Account → Sécurité → Mots de passe des applications (2FA requis)

### 3. Démarrer le site
```bash
docker compose up -d
```

### 4. Cron (lundi 4h)
```bash
crontab -e
# Ajouter :
0 4 * * 1 /home/alexis/Perso/Chess_Analysis/analysis/venv/bin/python /home/alexis/Perso/Chess_Analysis/analysis/weekly_analysis.py >> /var/log/chess_analysis.log 2>&1
```

### 5. Cloudflare
Dans le dashboard Cloudflare Tunnel → ajouter un hostname public :
- **Subdomain** : `chess`
- **Domain** : `alexisbousquet.fr`
- **Service** : `http://localhost:8765`

## Lancer manuellement
```bash
source analysis/venv/bin/activate
python analysis/weekly_analysis.py
```
