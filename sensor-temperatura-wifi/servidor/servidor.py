#!/usr/bin/env python3
"""Servidor de monitoramento dos sensores de temperatura (somente biblioteca padrão).

Recebe os lotes enviados pelos sensores, grava em SQLite, mostra um painel web,
exporta CSV e envia alertas (Telegram opcional) para:
  - temperatura fora da faixa (o sensor avisa na hora, sem esperar o lote);
  - sensor sem comunicação (bateria acabou, Wi-Fi caiu, sensor removido);
  - bateria baixa.

Uso:
  SENSOR_TOKEN=troque-este-token python3 servidor.py --porta 8080
Variáveis opcionais: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, BATERIA_BAIXA_MV (padrão 3700).
"""

import argparse
import csv
import html
import io
import json
import os
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("SENSOR_TOKEN", "troque-este-token")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
BATERIA_BAIXA_MV = int(os.environ.get("BATERIA_BAIXA_MV", "3700"))
FOLGA_OFFLINE_S = 15 * 60  # tolerância além do intervalo de envio antes de acusar "sem comunicação"

ERRO_SENSOR = 0x01

db_lock = threading.Lock()


def abrir_db(caminho):
    db = sqlite3.connect(caminho, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS sensores (
            id TEXT PRIMARY KEY,
            nome TEXT,
            fw TEXT,
            tmin REAL, tmax REAL,
            intervalo_envio_s INTEGER,
            ultimo_contato REAL,
            bat_mv INTEGER, rssi INTEGER,
            alarme INTEGER DEFAULT 0,
            offline INTEGER DEFAULT 0,
            bateria_baixa INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS leituras (
            sensor_id TEXT, boot INTEGER, t_dev INTEGER,
            ts REAL, temp REAL, umid INTEGER, flags INTEGER,
            PRIMARY KEY (sensor_id, boot, t_dev)
        );
        CREATE INDEX IF NOT EXISTS idx_leituras_ts ON leituras(sensor_id, ts);
        CREATE TABLE IF NOT EXISTS eventos (
            ts REAL, sensor_id TEXT, tipo TEXT, mensagem TEXT
        );
        """
    )
    return db


def alertar(db, sensor_id, tipo, mensagem):
    agora = time.time()
    db.execute("INSERT INTO eventos VALUES (?,?,?,?)", (agora, sensor_id, tipo, mensagem))
    db.commit()
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ALERTA {tipo}: {mensagem}", flush=True)
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        dados = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": mensagem}).encode()
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        threading.Thread(target=_post_silencioso, args=(url, dados), daemon=True).start()


def _post_silencioso(url, dados):
    try:
        urllib.request.urlopen(url, dados, timeout=10).read()
    except Exception as e:  # noqa: BLE001 - alerta não pode derrubar o servidor
        print(f"Falha ao enviar alerta pelo Telegram: {e}", flush=True)


def nome_sensor(row, sensor_id):
    return (row["nome"] if row and row["nome"] else None) or sensor_id


def registrar_lote(db, p):
    recebido = time.time()
    sid = str(p["id"])
    boot = int(p["boot"])
    agora_dev = int(p["agora"])
    leituras = p.get("leituras", [])

    with db_lock:
        ant = db.execute("SELECT * FROM sensores WHERE id=?", (sid,)).fetchone()
        nome = nome_sensor(ant, sid)
        for t_dev, c100, umid, flags in leituras:
            ts = recebido - (agora_dev - int(t_dev))
            temp = None if flags & ERRO_SENSOR else c100 / 100.0
            db.execute(
                "INSERT OR IGNORE INTO leituras VALUES (?,?,?,?,?,?,?)",
                (sid, boot, int(t_dev), ts, temp, int(umid), int(flags)),
            )

        alarme = bool(p.get("alarme"))
        bat = int(p.get("bat_mv", 0))
        db.execute(
            """INSERT INTO sensores (id, fw, tmin, tmax, intervalo_envio_s, ultimo_contato,
                                     bat_mv, rssi, alarme, offline)
               VALUES (?,?,?,?,?,?,?,?,?,0)
               ON CONFLICT(id) DO UPDATE SET fw=excluded.fw, tmin=excluded.tmin,
                   tmax=excluded.tmax, intervalo_envio_s=excluded.intervalo_envio_s,
                   ultimo_contato=excluded.ultimo_contato, bat_mv=excluded.bat_mv,
                   rssi=excluded.rssi, alarme=excluded.alarme, offline=0""",
            (sid, p.get("fw"), p.get("tmin"), p.get("tmax"), p.get("intervalo_envio_s"),
             recebido, bat, p.get("rssi"), int(alarme)),
        )
        db.commit()

        ultima = leituras[-1] if leituras else None
        if ultima and ultima[3] & ERRO_SENSOR:
            txt_temp = "erro na leitura do sensor"
        elif ultima:
            txt_temp = f"{ultima[1] / 100:.1f} °C"
        else:
            txt_temp = "sem leitura"

        if ant is None:
            alertar(db, sid, "novo", f"Novo sensor conectado: {sid} ({txt_temp})")
        elif ant["offline"]:
            alertar(db, sid, "online", f"{nome} voltou a se comunicar ({txt_temp})")

        if alarme and (ant is None or not ant["alarme"]):
            alertar(db, sid, "alarme",
                    f"🚨 {nome}: temperatura fora da faixa "
                    f"({p.get('tmin')} a {p.get('tmax')} °C): {txt_temp}")
        elif not alarme and ant is not None and ant["alarme"]:
            alertar(db, sid, "normal", f"✅ {nome}: temperatura normalizada ({txt_temp})")

        baixa = 0 < bat < BATERIA_BAIXA_MV
        if baixa and (ant is None or not ant["bateria_baixa"]):
            alertar(db, sid, "bateria", f"🔋 {nome}: bateria baixa ({bat / 1000:.2f} V), programe a troca")
        db.execute("UPDATE sensores SET bateria_baixa=? WHERE id=?", (int(baixa), sid))
        db.commit()


def vigiar_offline(db):
    while True:
        time.sleep(60)
        agora = time.time()
        with db_lock:
            for s in db.execute("SELECT * FROM sensores WHERE offline=0").fetchall():
                limite = (s["intervalo_envio_s"] or 3600) * 2 + FOLGA_OFFLINE_S
                if agora - (s["ultimo_contato"] or 0) > limite:
                    db.execute("UPDATE sensores SET offline=1 WHERE id=?", (s["id"],))
                    db.commit()
                    minutos = int((agora - s["ultimo_contato"]) / 60)
                    alertar(db, s["id"], "offline",
                            f"⚠️ {nome_sensor(s, s['id'])}: sem comunicação há {minutos} min")


# ---------------------------------------------------------------------------
# Painel web
# ---------------------------------------------------------------------------
def sparkline(pontos, tmin, tmax, largura=240, altura=40):
    vals = [t for _, t in pontos if t is not None]
    if len(vals) < 2:
        return ""
    lo = min(vals + ([tmin] if tmin is not None else []))
    hi = max(vals + ([tmax] if tmax is not None else []))
    if hi - lo < 1:
        hi, lo = hi + 0.5, lo - 0.5
    t0, t1 = pontos[0][0], pontos[-1][0]
    dt = (t1 - t0) or 1

    def y(v):
        return altura - (v - lo) / (hi - lo) * altura

    caminho = " ".join(
        f"{(ts - t0) / dt * largura:.1f},{y(t):.1f}" for ts, t in pontos if t is not None
    )
    faixa = ""
    if tmin is not None and tmax is not None:
        faixa = (f'<rect x="0" y="{y(tmax):.1f}" width="{largura}" '
                 f'height="{y(tmin) - y(tmax):.1f}" class="faixa"/>')
    return (f'<svg width="{largura}" height="{altura}" viewBox="0 0 {largura} {altura}">'
            f'{faixa}<polyline points="{caminho}" class="linha"/></svg>')


def pagina_painel(db):
    agora = time.time()
    linhas = []
    with db_lock:
        sensores = db.execute("SELECT * FROM sensores ORDER BY COALESCE(nome, id)").fetchall()
        for s in sensores:
            pts = db.execute(
                "SELECT ts, temp FROM leituras WHERE sensor_id=? AND ts>? ORDER BY ts",
                (s["id"], agora - 86400),
            ).fetchall()
            ultima = pts[-1]["temp"] if pts else None
            if s["offline"]:
                status, classe = "SEM COMUNICAÇÃO", "off"
            elif s["alarme"]:
                status, classe = "ALARME", "alarme"
            else:
                status, classe = "OK", "ok"
            idade = int((agora - (s["ultimo_contato"] or agora)) / 60)
            linhas.append(
                f"<tr class='{classe}'><td><b>{html.escape(nome_sensor(s, s['id']))}</b>"
                f"<br><small>{s['id']}</small></td>"
                f"<td class='temp'>{'—' if ultima is None else f'{ultima:.1f} °C'}</td>"
                f"<td>{status}</td><td>{s['tmin']} a {s['tmax']} °C</td>"
                f"<td>{sparkline([(p['ts'], p['temp']) for p in pts], s['tmin'], s['tmax'])}</td>"
                f"<td>{(s['bat_mv'] or 0) / 1000:.2f} V</td><td>{s['rssi']} dBm</td>"
                f"<td>há {idade} min</td>"
                f"<td><a href='/csv?sensor={s['id']}&dias=30'>CSV</a></td></tr>"
            )
        eventos = db.execute("SELECT * FROM eventos ORDER BY ts DESC LIMIT 20").fetchall()

    ev_html = "".join(
        f"<li>{time.strftime('%d/%m %H:%M', time.localtime(e['ts']))} — {html.escape(e['mensagem'])}</li>"
        for e in eventos
    )
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60"><title>Monitor de Temperatura</title>
<style>
 body{{font-family:system-ui,sans-serif;margin:16px;background:#f6f7f9;color:#1d2330}}
 table{{border-collapse:collapse;width:100%;background:#fff}}
 td,th{{padding:8px;border-bottom:1px solid #e3e6ea;text-align:left;vertical-align:middle}}
 .temp{{font-size:1.4em;font-weight:600}}
 tr.alarme td{{background:#fde2e1}} tr.off td{{background:#fff1cc}}
 .faixa{{fill:#d9f2e3}} .linha{{fill:none;stroke:#2563eb;stroke-width:1.5}}
 .tabela{{overflow-x:auto}}
</style></head><body>
<h1>Monitor de Temperatura</h1>
<div class="tabela"><table><tr><th>Sensor</th><th>Temp.</th><th>Status</th><th>Faixa</th>
<th>Últimas 24 h</th><th>Bateria</th><th>Sinal</th><th>Contato</th><th></th></tr>
{''.join(linhas) or '<tr><td colspan=9>Nenhum sensor ainda.</td></tr>'}
</table></div>
<h2>Eventos</h2><ul>{ev_html}</ul>
<p><small>Renomeie um sensor: POST /api/nome {{"id": "...", "nome": "Câmara 1"}}</small></p>
</body></html>"""


def gerar_csv(db, sensor_id, dias):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["data_hora", "sensor", "temperatura_c", "umidade_pct", "erro_sensor"])
    with db_lock:
        rows = db.execute(
            "SELECT ts, temp, umid, flags FROM leituras WHERE sensor_id=? AND ts>? ORDER BY ts",
            (sensor_id, time.time() - dias * 86400),
        ).fetchall()
    for r in rows:
        w.writerow([
            time.strftime("%d/%m/%Y %H:%M:%S", time.localtime(r["ts"])), sensor_id,
            "" if r["temp"] is None else f"{r['temp']:.2f}".replace(".", ","),
            r["umid"], "sim" if r["flags"] & ERRO_SENSOR else "",
        ])
    return buf.getvalue()


def criar_handler(db):
    class Handler(BaseHTTPRequestHandler):
        def _responder(self, codigo, corpo=b"", tipo="text/plain; charset=utf-8", extra=None):
            if isinstance(corpo, str):
                corpo = corpo.encode()
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(corpo)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(corpo)

        def _ler_json(self):
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")

        def do_POST(self):
            if self.headers.get("X-Token") != TOKEN:
                return self._responder(401, "token inválido")
            try:
                if self.path == "/api/leituras":
                    registrar_lote(db, self._ler_json())
                    return self._responder(200, "ok")
                if self.path == "/api/nome":
                    p = self._ler_json()
                    with db_lock:
                        db.execute("UPDATE sensores SET nome=? WHERE id=?", (p["nome"], p["id"]))
                        db.commit()
                    return self._responder(200, "ok")
            except (ValueError, KeyError, TypeError) as e:
                return self._responder(400, f"requisição inválida: {e}")
            self._responder(404, "não encontrado")

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(url.query)
            if url.path == "/":
                return self._responder(200, pagina_painel(db), "text/html; charset=utf-8")
            if url.path == "/csv" and "sensor" in q:
                sid = q["sensor"][0]
                dias = int(q.get("dias", ["30"])[0])
                return self._responder(
                    200, gerar_csv(db, sid, dias), "text/csv; charset=utf-8",
                    {"Content-Disposition": f'attachment; filename="{sid}.csv"'},
                )
            if url.path == "/api/sensores":
                with db_lock:
                    rows = [dict(r) for r in db.execute("SELECT * FROM sensores")]
                return self._responder(200, json.dumps(rows), "application/json")
            self._responder(404, "não encontrado")

        def log_message(self, fmt, *args):
            pass

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--porta", type=int, default=8080)
    ap.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "monitor.db"))
    args = ap.parse_args()

    db = abrir_db(args.db)
    threading.Thread(target=vigiar_offline, args=(db,), daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", args.porta), criar_handler(db))
    print(f"Painel em http://0.0.0.0:{args.porta}/", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
