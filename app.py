from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import json
import uuid
from datetime import datetime
from google.cloud import storage

app = FastAPI()

# ----------------------------
# HABILITAR CORS PARA GEOIPT.CL
# ----------------------------
origins = [
    "https://geoipt.cl",
    "https://www.geoipt.cl",
    "https://geocalculo.github.io",   # si usas preview en GitHub Pages
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


import os
import json
from datetime import datetime, timezone
from uuid import uuid4
from flask import Flask, request, jsonify
from google.cloud import storage
import smtplib
from email.message import EmailMessage
from io import StringIO
import csv

app = Flask(__name__)

# === CONFIGURACIÓN vía variables de entorno ===
LOGS_BUCKET = os.environ.get("LOGS_BUCKET")          # p.ej. "geoipt-logs"
SMTP_USER   = os.environ.get("SMTP_USER")            # p.ej. "tucorreo@gmail.com"
SMTP_PASS   = os.environ.get("SMTP_PASS")            # app password de Gmail
SMTP_TO     = os.environ.get("SMTP_TO")              # "geocalculo@gmail.com"

storage_client = storage.Client()


def _get_bucket():
    if not LOGS_BUCKET:
        raise RuntimeError("LOGS_BUCKET no está configurado")
    return storage_client.bucket(LOGS_BUCKET)


def _today_str():
    # Usamos UTC para simplificar; si quieres, luego lo cambiamos a America/Santiago
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@app.post("/api/log_evento")
def log_evento():
    """
    Guarda un evento individual en GCS como JSON.
    Se llamará desde index.html / info.html vía fetch().
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        tipo = data.get("tipo", "desconocido")
        detalle = data.get("detalle", {})

        now = datetime.now(timezone.utc)
        fecha_str = now.isoformat()

        # Info de request
        ip = request.headers.get("X-Forwarded-For", request.remote_addr)
        user_agent = request.headers.get("User-Agent", "")

        evento = {
            "tipo": tipo,
            "detalle": detalle,
            "fecha": fecha_str,
            "ip": ip,
            "user_agent": user_agent,
        }

        # Guardar como JSON en un blob por evento:
        # events/AAAA-MM-DD/AAAA-MM-DDTHHMMSS_xxx.json
        date_folder = _today_str()
        ts_compacto = now.strftime("%Y%m%dT%H%M%S")
        random_suffix = uuid4().hex[:6]
        blob_name = f"events/{date_folder}/{ts_compacto}_{random_suffix}.json"

        bucket = _get_bucket()
        blob = bucket.blob(blob_name)
        blob.upload_from_string(json.dumps(evento), content_type="application/json")

        return jsonify({"ok": True}), 200

    except Exception as e:
        # No queremos romper la app si falla el log
        return jsonify({"ok": False, "error": str(e)}), 500


def _leer_eventos_fecha(fecha_str: str):
    """
    Lee todos los eventos de una fecha dada (AAAA-MM-DD) desde GCS.
    """
    bucket = _get_bucket()
    prefix = f"events/{fecha_str}/"
    blobs = bucket.list_blobs(prefix=prefix)

    eventos = []
    for blob in blobs:
        try:
            contenido = blob.download_as_text()
            evento = json.loads(contenido)
            eventos.append(evento)
        except Exception:
            continue
    return eventos


def _construir_resumen_y_csv(eventos, fecha_str: str):
    """
    Construye un resumen simple y un CSV (como texto) desde la lista de eventos.
    """
    total = len(eventos)
    por_tipo = {}
    for ev in eventos:
        t = ev.get("tipo", "desconocido")
        por_tipo[t] = por_tipo.get(t, 0) + 1

    # CSV en memoria
    output = StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["fecha", "tipo", "ip", "detalle", "user_agent"])
    for ev in eventos:
        writer.writerow([
            ev.get("fecha", ""),
            ev.get("tipo", ""),
            ev.get("ip", ""),
            json.dumps(ev.get("detalle", {}), ensure_ascii=False),
            ev.get("user_agent", ""),
        ])
    csv_text = output.getvalue()

    resumen = {
        "fecha": fecha_str,
        "total_eventos": total,
        "por_tipo": por_tipo,
    }
    return resumen, csv_text


def _enviar_correo_resumen(resumen, csv_text, fecha_str: str):
    """
    Envía un correo con el resumen y adjunta el CSV.
    """
    if not (SMTP_USER and SMTP_PASS and SMTP_TO):
        # Si no hay configuración, solo salimos
        return

    asunto = f"GeoIPT – Resumen eventos {fecha_str}"
    cuerpo = [
        f"Resumen de eventos GeoIPT para el día {fecha_str}",
        "",
        f"Total de eventos: {resumen['total_eventos']}",
        "",
        "Detalle por tipo:",
    ]
    for tipo, cantidad in resumen["por_tipo"].items():
        cuerpo.append(f"- {tipo}: {cantidad}")
    cuerpo.append("")
    cuerpo.append("Se adjunta archivo CSV con el detalle de los eventos.")

    msg = EmailMessage()
    msg["Subject"] = asunto
    msg["From"] = SMTP_USER
    msg["To"] = SMTP_TO
    msg.set_content("\n".join(cuerpo))

    # Adjuntar CSV
    filename = f"geoipt_eventos_{fecha_str}.csv"
    msg.add_attachment(
        csv_text.encode("utf-8"),
        maintype="text",
        subtype="csv",
        filename=filename,
    )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg)


@app.get("/api/resumen_diario")
def resumen_diario():
    """
    Endpoint que usará Cloud Scheduler.
    Parámetro opcional ?fecha=AAAA-MM-DD, si no -> hoy (UTC).
    """
    fecha_str = request.args.get("fecha") or _today_str()

    eventos = _leer_eventos_fecha(fecha_str)
    resumen, csv_text = _construir_resumen_y_csv(eventos, fecha_str)

    # Intentar enviar correo (si está configurado)
    try:
        _enviar_correo_resumen(resumen, csv_text, fecha_str)
        resumen["email_enviado"] = True
    except Exception as e:
        resumen["email_enviado"] = False
        resumen["email_error"] = str(e)

    return jsonify(resumen), 200


# Para desarrollo local
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
