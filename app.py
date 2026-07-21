import os
import time
import requests
import json
from threading import Thread
from flask import Flask
import firebase_admin
from firebase_admin import credentials, firestore
from scipy.stats import poisson

app = Flask(__name__)

# --- 1. CONFIGURACIÓN DE FIREBASE PARA RENDER ---
# En Render, NO subas tu archivo JSON de Firebase. 
# Guarda el contenido del JSON en una Variable de Entorno llamada 'FIREBASE_CREDENTIALS'.
firebase_creds_json = os.environ.get('FIREBASE_CREDENTIALS')

if firebase_creds_json and not firebase_admin._apps:
    creds_dict = json.loads(firebase_creds_json)
    cred = credentials.Certificate(creds_dict)
    firebase_admin.initialize_app(cred)

db = firestore.client() if firebase_creds_json else None

# --- 2. MATEMÁTICA: POISSON PRE-MATCH ---
def analizar_valor_esperado(linea_goles, cuota_ofrecida, prom_historico_goles):
    # En pre-match, el lambda es simplemente el promedio histórico del torneo/jugador
    lambda_partido = prom_historico_goles
    
    # Goles enteros necesarios para ganar el Over (ej. si la línea es 8.5, necesitas 9)
    goles_necesarios = int(linea_goles) + 1
    
    # Probabilidad de superar la línea: P(X >= goles_necesarios)
    prob_over = 1 - poisson.cdf(goles_necesarios - 1, lambda_partido)
    
    # Valor esperado: (Probabilidad * Cuota) - 1
    ev = (prob_over * cuota_ofrecida) - 1
    
    return round(prob_over, 4), round(ev, 4)

# --- 3. LÓGICA DE EXTRACCIÓN (SCRAPER DE API) ---
def buscar_y_analizar_partidos():
    print("Iniciando búsqueda de partidos Pre-Match...")
    # Esta es la URL de Kambi para partidos que están por comenzar (visto en tu archivo HAR)
    url_kambi = "https://us.offering-api.kambicdn.com/offering/v2018/nexuspe/listView/all/all/all/all/starting-within.json?lang=es_PE&market=PE&client_id=200"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    }

    try:
        response = requests.get(url_kambi, headers=headers)
        if response.status_code != 200:
            print(f"Error consultando Kambi: {response.status_code}")
            return

        data = response.json()
        
        for item in data.get("events", []):
            evento = item.get("event", {})
            nombre_grupo = evento.get("group", "")
            
            # Filtrar solo eSports (4, 5 o 6 minutos)
            if "Esports" not in nombre_grupo and "Cyber" not in nombre_grupo:
                continue

            partido_id = str(evento.get("id"))
            partido_info = {
                "nombre": evento.get("name"),
                "liga": nombre_grupo,
                "inicio": evento.get("start"),
                "estado": evento.get("state")
            }

            # Buscar la cuota de Más/Menos Goles (Total Goals)
            for offer in item.get("betOffers", []):
                if offer.get("betOfferType", {}).get("id") == 6: # ID 6 = Más/Menos de
                    for outcome in offer.get("outcomes", []):
                        if outcome.get("type") == "OT_OVER":
                            linea = outcome.get("line") / 1000  # Ej: 8500 -> 8.5
                            cuota = outcome.get("odds") / 1000  # Ej: 2050 -> 2.05
                            
                            # AQUÍ DEFINES TU ESTADÍSTICA BASE
                            # Si es de 4x5min o 2x6min, ajusta este promedio según tu investigación
                            promedio_historico = 8.5 
                            
                            prob, ev = analizar_valor_esperado(linea, cuota, promedio_historico)
                            
                            partido_info["linea_over"] = linea
                            partido_info["cuota_over"] = cuota
                            partido_info["probabilidad"] = prob
                            partido_info["EV"] = ev
                            
                            if db:
                                # Guardar en Firebase usando el ID del partido
                                db.collection("predicciones_prematch").document(partido_id).set(partido_info)
                                print(f"✅ Analizado y guardado: {partido_info['nombre']} | EV: {ev}")

    except Exception as e:
        print(f"Error en el ciclo de análisis: {e}")

# --- 4. BUCLE EN SEGUNDO PLANO ---
def loop_infinito():
    while True:
        buscar_y_analizar_partidos()
        # Esperar 5 minutos (300 segundos) antes de volver a buscar para no saturar la API
        time.sleep(300)

# --- 5. SERVIDOR WEB (PARA MANTENER DESPIERTO A RENDER) ---
@app.route('/')
def home():
    return "🤖 Agente de Análisis Pre-Match Activo 24/7."

if __name__ == '__main__':
    # Iniciar el bot analista en un hilo separado
    hilo_bot = Thread(target=loop_infinito)
    hilo_bot.daemon = True
    hilo_bot.start()

    # Iniciar el servidor web que Render necesita
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
