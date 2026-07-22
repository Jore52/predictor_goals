import os
import time
import requests
import json
import re
from threading import Thread
from flask import Flask, request, jsonify
from flask_cors import CORS  # <--- IMPORTAMOS CORS
import firebase_admin
from firebase_admin import credentials, firestore
from scipy.stats import poisson

app = Flask(__name__)
CORS(app) # <--- ESTA LÍNEA MÁGICA PERMITE QUE OLIMPO TE ENVÍE DATOS

# =====================================================================
# 1. CONFIGURACIÓN DE FIREBASE
# =====================================================================
firebase_creds_json = os.environ.get('FIREBASE_CREDENTIALS')

if firebase_creds_json and not firebase_admin._apps:
    creds_dict = json.loads(firebase_creds_json)
    cred = credentials.Certificate(creds_dict)
    firebase_admin.initialize_app(cred)

db = firestore.client() if firebase_creds_json else None


# =====================================================================
# 2. MODELO MATEMÁTICO (POISSON)
# =====================================================================
def analizar_valor_esperado(linea_goles, cuota_ofrecida, lambda_partido):
    # Goles enteros necesarios para ganar el Over (ej. línea 8.5 -> necesitas 9)
    goles_necesarios = int(linea_goles) + 1
    
    # Probabilidad de superar la línea: P(X >= goles_necesarios)
    prob_over = 1 - poisson.cdf(goles_necesarios - 1, lambda_partido)
    
    # Valor Esperado (EV)
    ev = (prob_over * cuota_ofrecida) - 1
    
    return round(prob_over, 4), round(ev, 4)


# =====================================================================
# 3. EXTRACCIÓN DE SEUDÓNIMOS (Regex)
# =====================================================================
def extraer_jugador(nombre_equipo):
    # Busca el texto entre paréntesis: "River Plate (Kosta)" -> "Kosta"
    match = re.search(r'\(([^)]+)\)', nombre_equipo)
    return match.group(1).strip() if match else nombre_equipo.strip()


# =====================================================================
# 4. MOTOR DE ANÁLISIS PRE-MATCH (Se ejecuta cada 5 min)
# =====================================================================
def buscar_y_analizar_partidos():
    if not db:
        print("Esperando conexión a Firebase...")
        return

    print("Buscando partidos Pre-Match en Kambi...")
    url_kambi = "https://us.offering-api.kambicdn.com/offering/v2018/nexuspe/listView/all/all/all/all/starting-within.json?lang=es_PE&market=PE&client_id=200"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    try:
        response = requests.get(url_kambi, headers=headers)
        if response.status_code != 200:
            return

        data = response.json()
        
        for item in data.get("events", []):
            evento = item.get("event", {})
            nombre_grupo = evento.get("group", "")
            
            # Filtramos solo eSports de corta duración
            if "Esports" not in nombre_grupo and "Cyber" not in nombre_grupo:
                continue

            partido_id = str(evento.get("id"))
            jugador_local = extraer_jugador(evento.get("homeName", ""))
            jugador_visitante = extraer_jugador(evento.get("awayName", ""))

            partido_info = {
                "nombre": evento.get("name"),
                "liga": nombre_grupo,
                "inicio": evento.get("start"),
                "jugador_local": jugador_local,
                "jugador_visitante": jugador_visitante
            }

            # --- CONSULTAR ESTADÍSTICAS HISTÓRICAS EN FIREBASE ---
            doc_local = db.collection("estadisticas_jugadores").document(jugador_local).get()
            doc_visitante = db.collection("estadisticas_jugadores").document(jugador_visitante).get()

            # Lógica para predecir goles combinando el ataque de uno y la defensa del otro
            if doc_local.exists and doc_visitante.exists:
                stats_l = doc_local.to_dict()
                stats_v = doc_visitante.to_dict()
                
                exp_goles_local = (stats_l.get("promedio_a_favor", 0) + stats_v.get("promedio_en_contra", 0)) / 2
                exp_goles_visitante = (stats_v.get("promedio_a_favor", 0) + stats_l.get("promedio_en_contra", 0)) / 2
                lambda_calculado = exp_goles_local + exp_goles_visitante
            else:
                lambda_calculado = 8.5 # Valor por defecto si no hay historial

            partido_info["lambda_esperado"] = round(lambda_calculado, 2)

            # --- BUSCAR CUOTAS OVER/UNDER ---
            for offer in item.get("betOffers", []):
                if offer.get("betOfferType", {}).get("id") == 6: # ID 6 = Mercado Total Goles
                    for outcome in offer.get("outcomes", []):
                        if outcome.get("type") == "OT_OVER":
                            linea = outcome.get("line") / 1000  # Ej: 8500 -> 8.5
                            cuota = outcome.get("odds") / 1000  # Ej: 2050 -> 2.05
                            
                            prob, ev = analizar_valor_esperado(linea, cuota, lambda_calculado)
                            
                            partido_info["linea_over"] = linea
                            partido_info["cuota_over"] = cuota
                            partido_info["probabilidad"] = prob
                            partido_info["EV"] = ev
                            
                            # Guardar oportunidad
                            db.collection("predicciones_prematch").document(partido_id).set(partido_info)
                            print(f"✅ Guardado: {partido_info['nombre']} | EV: {ev}")

    except Exception as e:
        print(f"Error en análisis: {e}")


# =====================================================================
# 5. BUCLE EN SEGUNDO PLANO
# =====================================================================
def loop_infinito():
    while True:
        buscar_y_analizar_partidos()
        time.sleep(300) # Espera 5 minutos


# =====================================================================
# 6. ENDPOINTS FLASK (API PARA LA EXTENSIÓN)
# =====================================================================
@app.route('/')
def home():
    return "🤖 API de Análisis de eSports (Olimpo) Activa 24/7."

# La extensión de tu navegador enviará los marcadores a esta ruta
@app.route('/actualizar_estadisticas', methods=['POST', 'OPTIONS'])
def actualizar_estadisticas():
    # Permitir peticiones CORS desde la extensión del navegador
    if request.method == 'OPTIONS':
        headers = {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'POST',
            'Access-Control-Allow-Headers': 'Content-Type'
        }
        return ('', 204, headers)

    if not db:
        return jsonify({"error": "Firebase no configurado"}), 500

    datos = request.json
    jugador = datos.get("jugador")
    goles_a_favor = int(datos.get("goles_a_favor", 0))
    goles_en_contra = int(datos.get("goles_en_contra", 0))

    if not jugador:
        return jsonify({"error": "Falta el nombre del jugador"}), 400

    try:
        ref_jugador = db.collection("estadisticas_jugadores").document(jugador)
        doc = ref_jugador.get()

        if doc.exists:
            stats = doc.to_dict()
            nuevos_partidos = stats.get("partidos_jugados", 0) + 1
            nuevos_favor = stats.get("goles_a_favor_totales", 0) + goles_a_favor
            nuevos_contra = stats.get("goles_en_contra_totales", 0) + goles_en_contra
        else:
            nuevos_partidos = 1
            nuevos_favor = goles_a_favor
            nuevos_contra = goles_en_contra

        # Recalcular promedios
        prom_favor = nuevos_favor / nuevos_partidos
        prom_contra = nuevos_contra / nuevos_partidos

        # Guardar en Firebase
        ref_jugador.set({
            "partidos_jugados": nuevos_partidos,
            "goles_a_favor_totales": nuevos_favor,
            "goles_en_contra_totales": nuevos_contra,
            "promedio_a_favor": round(prom_favor, 2),
            "promedio_en_contra": round(prom_contra, 2),
            "ultima_actualizacion": firestore.SERVER_TIMESTAMP
        })

        return jsonify({"mensaje": f"Estadísticas actualizadas para {jugador}"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# =====================================================================
# INICIALIZACIÓN
# =====================================================================
if __name__ == '__main__':
    # Iniciar el hilo recolector de partidos
    hilo_bot = Thread(target=loop_infinito)
    hilo_bot.daemon = True
    hilo_bot.start()

    # Iniciar Flask
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
