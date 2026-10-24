"""Bot de value bets (football).

Principe :
  1. On récupère les cotes de Pinnacle (référence très précise) et de 1xBet via The Odds API.
  2. On retire la marge de Pinnacle pour estimer la vraie probabilité de chaque issue.
  3. Si la cote 1xBet est nettement supérieure à la cote "juste", c'est un value bet.
  4. On envoie au maximum 2 paris par jour sur Telegram, avec la cote minimale à accepter.

Usage :
  python valuebets.py          # analyse et envoie le résultat sur Telegram
  python valuebets.py --dry    # analyse et affiche, sans envoyer

Aucun pari n'est garanti : un value bet est un pari dont la cote est supérieure à la
probabilité estimée, il peut quand même perdre.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import requests

# ----------------------------- RÉGLAGES ------------------------------------
API = "https://api.the-odds-api.com/v4"

# clé The Odds API -> nom affiché (d'autres clés : soccer_italy_serie_a,
# soccer_germany_bundesliga, soccer_uefa_champs_league, basketball_nba ...)
LEAGUES = {
    "soccer_epl": "Premier League",
    "soccer_spain_la_liga": "Liga",
    "soccer_france_ligue_one": "Ligue 1",
}

SHARP = "pinnacle"      # bookmaker de référence (cotes les plus précises)
SOFT = "onexbet"        # bookmaker comparé (1xBet)
HOURS_AHEAD = 24        # matchs qui commencent dans les 24 prochaines heures
MIN_EDGE = 0.03         # avantage minimal : +3 %
MAX_EDGE = 0.20         # au-delà de +20 %, c'est presque toujours une cote erronée ou périmée
MIN_ODDS, MAX_ODDS = 1.50, 4.00   # on évite les cotes très basses et les très gros outsiders
MAX_SHARP_MARGIN = 0.08  # marché Pinnacle ignoré si sa marge dépasse 8 % (marché douteux)
MAX_PICKS = 2
# ---------------------------------------------------------------------------


def devig_power(odds):
    """Retire la marge du bookmaker (méthode de la puissance). Retourne les probabilités."""
    p = [1 / o for o in odds]
    s = sum(p)
    if s <= 1:
        return [x / s for x in p]
    lo, hi = 1.0, 10.0
    for _ in range(60):
        k = (lo + hi) / 2
        if sum(x ** k for x in p) > 1:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    q = [x ** k for x in p]
    t = sum(q)
    return [x / t for x in q]


def prices(event, book):
    """Cotes 'vainqueur du match' d'un bookmaker pour un événement : {issue: cote}."""
    for b in event.get("bookmakers", []):
        if b.get("key") == book:
            for m in b.get("markets", []):
                if m.get("key") == "h2h":
                    return {o["name"]: float(o["price"]) for o in m["outcomes"]}
    return None


def find_value_bets(events, league, now):
    """Retourne les value bets (au plus un par match), du meilleur au moins bon."""
    picks = []
    limit = now + timedelta(hours=HOURS_AHEAD)
    for ev in events:
        start = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        if not (now < start <= limit):
            continue
        sharp, soft = prices(ev, SHARP), prices(ev, SOFT)
        if not sharp or not soft or set(sharp) != set(soft) or len(sharp) < 2:
            continue
        names = list(sharp)
        margin = sum(1 / sharp[n] for n in names) - 1
        if margin > MAX_SHARP_MARGIN:
            continue
        fair = dict(zip(names, devig_power([sharp[n] for n in names])))

        best = None
        for n in names:
            odds = soft[n]
            edge = fair[n] * odds - 1
            if not (MIN_EDGE <= edge <= MAX_EDGE and MIN_ODDS <= odds <= MAX_ODDS):
                continue
            if best is None or edge > best["edge"]:
                best = dict(
                    league=league,
                    home=ev["home_team"],
                    away=ev["away_team"],
                    start=start,
                    pick=n,
                    odds=odds,
                    fair_prob=fair[n],
                    min_odds=(1 + MIN_EDGE) / fair[n],
                    edge=edge,
                )
        if best:
            picks.append(best)
    picks.sort(key=lambda p: p["edge"], reverse=True)
    return picks


def fetch_events(sport, api_key):
    r = requests.get(
        f"{API}/sports/{sport}/odds",
        params={
            "apiKey": api_key,
            "bookmakers": f"{SHARP},{SOFT}",
            "markets": "h2h",
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json(), r.headers.get("x-requests-remaining")


def send_telegram(text):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise SystemExit("TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID manquant.")
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": text},
        timeout=20,
    )
    r.raise_for_status()


def build_message(p):
    what = "Match nul" if p["pick"] == "Draw" else f"Victoire de {p['pick']}"
    return (
        f"⚽ VALUE BET - {p['league']}\n"
        f"{p['home']} vs {p['away']}\n"
        f"Coup d'envoi : {p['start']:%d/%m %H:%M} UTC\n\n"
        f"Pari : {what}\n"
        f"Cote 1xBet : {p['odds']:.2f}\n"
        f"Probabilité estimée : {p['fair_prob']:.0%}\n"
        f"Avantage estimé : +{p['edge']:.1%}\n\n"
        f"Si vous pariez sur Betwinner : ne jouez que si la cote y est d'au moins {p['min_odds']:.2f}.\n"
        "Mise conseillée : petite et fixe (environ 1 % de votre capital).\n"
        "Aucun pari n'est garanti."
    )


def main():
    dry = "--dry" in sys.argv[1:]
    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        raise SystemExit("ODDS_API_KEY manquant.")

    now = datetime.now(timezone.utc)
    picks, errors, remaining = [], 0, None
    for sport, label in LEAGUES.items():
        try:
            events, remaining = fetch_events(sport, api_key)
        except Exception as e:  # un championnat en erreur ne bloque pas les autres
            errors += 1
            print(f"[{label}] erreur : {e}")
            continue
        found = find_value_bets(events, label, now)
        print(f"[{label}] {len(events)} matchs, {len(found)} value bet(s).")
        picks += found

    if errors == len(LEAGUES):
        raise SystemExit("Aucun championnat n'a pu être analysé.")

    picks.sort(key=lambda p: p["edge"], reverse=True)
    picks = picks[:MAX_PICKS]
    credits = f"\n\nCrédits API restants ce mois-ci : {remaining}" if remaining else ""

    messages = [build_message(p) for p in picks]
    if not messages:
        messages = ["Aucun value bet aujourd'hui : c'est normal, mieux vaut ne pas parier que forcer." + credits]
    else:
        messages[-1] += credits

    for m in messages:
        print(m, "\n")
        if not dry:
            send_telegram(m)


if __name__ == "__main__":
    main()
  
