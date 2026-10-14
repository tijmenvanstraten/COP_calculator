import appdaemon.plugins.hass.hassapi as hass
import pymysql
import math
import datetime
import os

class WoningThermischeAnalyse(hass.Hass):

    def _get_db_config(self):
        required = {
            "host": "HA_DB_HOST",
            "user": "HA_DB_USER",
            "database": "HA_DB_NAME",
        }
        db_config = {}
        missing = []
        for config_key, env_key in required.items():
            value = os.environ.get(env_key)
            if not value:
                missing.append(env_key)
            else:
                db_config[config_key] = value

        password_file = os.environ.get("HA_DB_PASSWORD_FILE")
        if password_file:
            with open(password_file, encoding="utf-8") as secret_file:
                password = secret_file.read().strip()
        else:
            password = os.environ.get("HA_DB_PASSWORD")
        if not password:
            missing.append("HA_DB_PASSWORD or HA_DB_PASSWORD_FILE")

        port_value = os.environ.get("HA_DB_PORT", "3306")
        try:
            port = int(port_value)
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError as err:
            raise ValueError("HA_DB_PORT must be an integer between 1 and 65535") from err

        if missing:
            raise ValueError(
                "Missing database configuration: " + ", ".join(missing)
            )

        db_config.update({"password": password, "port": port})
        return db_config

    def initialize(self):
        self.log("--- Start App Woning Thermische Analyse ---")
        # Draait elke nacht om 04:30
        self.run_daily(self.bereken_constantes, "04:30:00")
        # Ook eenmalig draaien bij opstarten voor testen
        #self.run_in(self.bereken_constantes, 10)

    def bereken_constantes(self, kwargs):
        # ENTITY CONFIGURATIE
        binnen_sensor = 'sensor.woonkamer_temperatuur_gemiddeld'
        buiten_sensor = 'sensor.kantoor_buiten_temperatuur'
        wp_status_sensor = 'sensor.control_unit_operation_state_2'
        zon_sensor = 'sensor.solaredge_i1_ac_power'
        wind_sensor = 'sensor.thuis_windsnelheid'

        # WP STATUS DEFINITIES
        wp_uit_lijst = ['operation_state_heat_thermo_off', 'operation_state_dhw_off', 'off']
        wp_aan_verwarmen = 'operation_state_heat_thermo_on'

        # Snelle query zonder subselecties
        query = f"""
        SELECT
            s.last_updated_ts,
            sm.entity_id,
            s.state
        FROM states s
        INNER JOIN states_meta sm ON s.metadata_id = sm.metadata_id
        WHERE sm.entity_id IN ('{binnen_sensor}', '{buiten_sensor}', '{wp_status_sensor}', '{zon_sensor}', '{wind_sensor}')
        AND s.last_updated_ts > UNIX_TIMESTAMP(NOW() - INTERVAL 10 DAY)
        AND s.state NOT IN ('unknown', 'unavailable', 'none')
        ORDER BY s.last_updated_ts ASC;
        """

        try:
            db_config = self._get_db_config()
            self.log("Verbinding maken met database...")
            conn = pymysql.connect(**db_config)
            self.log("Verbonden! Query uitvoeren...")
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                cursor.execute(query)
                rows = cursor.fetchall()
            conn.close()
            self.log(f"Query klaar. Aantal rijen opgehaald: {len(rows)}")
            
            if len(rows) == 0:
                self.log("Check je entity namen: de database geeft 0 rijen terug voor deze combinatie.")
                return
                
            k_afkoel, k_opwarm, k_solar, k_wind = [], [], [], []
            # Forward-fill variabelen (onthouden de laatste status)
            cur_binnen = cur_buiten = cur_wp = cur_zon = cur_wind = None
            last_ts = None

            for row in rows:
                ts = row['last_updated_ts']
                eid = row['entity_id']
                state = row['state']

                # Update de huidige status van de sensoren
                if eid == binnen_sensor:
                    b_oud = cur_binnen
                    try:
                        cur_binnen = float(state)
                    except ValueError: continue

                    # Berekening vindt alleen plaats bij een wijziging in binnentemperatuur
                    if b_oud is not None and last_ts is not None:
                        dt = (ts - last_ts) / 3600 # Tijdverschil in uren
                        
                        # Filter op realistische tijdsstappen (2 seconden tot 30 minuten)
                        if 0.0005 < dt < 0.5 and cur_buiten is not None and cur_wp is not None:
                            delta_t = b_oud - cur_buiten
                            if delta_t <= 0: delta_t = 0.1 # Voorkom delen door nul
                            
                            zon = cur_zon if cur_zon is not None else 0
                            wind = cur_wind if cur_wind is not None else 0

                            # 1. AFKOELING (Nacht/geen zon en weinig wind < 20 km/h)
                            if cur_wp in wp_uit_lijst and zon < 10 and wind < 20:
                                k = ((b_oud - cur_binnen) / dt) / delta_t
                                if 0.001 < k < 0.1: k_afkoel.append(k)

                            # 2. OPWARMEN (WP aan voor verwarming)
                            elif cur_wp == wp_aan_verwarmen:
                                basis_k = float(self.get_state("input_number.huis_afkoel_constante_helper", default=0.015))
                                stijging = ((cur_binnen - b_oud) / dt) + (delta_t * basis_k)
                                if 0.05 < stijging < 2.5: k_opwarm.append(stijging)

                            # 3. SOLAR GAIN (WP uit en zoninstraling > 200W)
                            if cur_wp in wp_uit_lijst and zon > 200:
                                basis_k = float(self.get_state("input_number.huis_afkoel_constante_helper", default=0.015))
                                winst = ((cur_binnen - b_oud) / dt) + (delta_t * basis_k)
                                k_s = winst / (zon / 1000)
                                if 0.01 < k_s < 2.0: k_solar.append(k_s)
                                
                            # 4. WIND LOSS (Extra verlies bij wind > 30 km/h)
                            if cur_wp in wp_uit_lijst and wind > 30 and zon < 10:
                                actuele_k = ((b_oud - cur_binnen) / dt) / delta_t
                                basis_k = float(self.get_state("input_number.huis_afkoel_constante_helper", default=0.015))
                                extra_k = (actuele_k - basis_k) / wind
                                if 0 < extra_k < 0.02: k_wind.append(extra_k)

                    last_ts = ts

                elif eid == buiten_sensor:
                    try: cur_buiten = float(state) 
                    except ValueError: pass
                elif eid == wp_status_sensor:
                    cur_wp = state
                elif eid == zon_sensor:
                    try: cur_zon = float(state)
                    except ValueError: pass
                elif eid == wind_sensor:
                    try: cur_wind = float(state)
                    except ValueError: pass

            # Opslaan van de medianen
            self.save_median("sensor.huis_afkoel_constante", k_afkoel, "K/h/ΔT")
            self.save_median("sensor.huis_opwarm_constante", k_opwarm, "K/h")
            self.save_median("sensor.huis_solar_gain_constante", k_solar, "K/h/kW")
            self.save_median("sensor.huis_wind_loss_factor", k_wind, "extra_K/h/ΔT/kmh")
            
            self.log("Analyse voltooid en sensoren bijgewerkt.")

        except Exception as e:
            self.log(f"Fout tijdens database-analyse: {e}", level="ERROR")

    def save_median(self, entity, values, unit):
        if not values:
            return
        values.sort()
        median = values[len(values)//2]
        afgeronde_waarde = round(median, 5)

        # Bepaal de naam van de helper (bijv. sensor.huis_afkoel_constante --> input_number.huis_afkoel_constante_helper)
        helper_entity = entity.replace("sensor.", "input_number.") + "_helper"
        
        # Schrijf de waarde naar de helper
        self.call_service("input_number/set_value", entity_id=helper_entity, value=afgeronde_waarde)
        
        # Update de extra attributen op de template sensor
        self.set_state(entity, state=afgeronde_waarde, attributes={
            "datapunten": len(values),
            "laatste_update": str(datetime.datetime.now())
        })
