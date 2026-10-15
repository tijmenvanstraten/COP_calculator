import appdaemon.plugins.hass.hassapi as hass
import datetime

class EMSWarmWater(hass.Hass):

    def initialize(self):
        self.log("--- EMS Warm Tapwater Gestart (V16 - Definitief Deel 1) ---")
        
        # Geleerde waarde initialiseren in HA/AppDaemon status als fallback
        if not self.entity_exists("sensor.dhw_geleerd_kwartierverlies"):
            self.set_state("sensor.dhw_geleerd_kwartierverlies", state="0.035", attributes={"friendly_name": "Geleerd DHW Kwartierverlies"})

        # Draai synchroon met de kwartiertarieven elke 15 minuten
        self.run_every(self.simuleer_dhw_behoefte, "now", 15 * 60)
        
        # Bereken elke ochtend om 05:01 het werkelijke stilstandsverlies van de nacht
        self.run_daily(self.bereken_historisch_stilstandsverlies, datetime.time(5, 1, 0))

    def bereken_historisch_stilstandsverlies(self, kwargs):
        """ Berekent het pure isolatieverlies tussen 01:00 en 05:00 zonder douche-invloeden """
        self.log("DHW LEARN: Starten van de nachtelijke verliesberekening...")
        
        start_tijd = self.get_now() - datetime.timedelta(hours=4)
        
        history_data = self.get_history(entity_id="sensor.dhw_current_temperature", start_time=start_tijd)
        status_data = self.get_history(entity_id="sensor.control_unit_operation_state_2", start_time=start_tijd)
        
        # VEILIGHEIDSCHECK: Controleer of de warmtepomp vannacht fysiek aan heeft gestaan voor DHW
        heeft_gedraaid = False
        if isinstance(status_data, list) and len(status_data) > 0:
            # Pak de lijst met states uit de eerste entiteit
            actual_status_list = status_data[0] if isinstance(status_data[0], list) else status_data
            for state_entry in actual_status_list:
                if state_entry.get("state") == "operation_state_dhw_on":
                    heeft_gedraaid = True
                    break

        if heeft_gedraaid:
            self.log("DHW LEARN: Warmtepomp heeft vannacht gedraaid tussen 01:00 en 05:00. Berekening overgeslagen.")
            return

        # VEILIGHEIDSCHECK: Bereken de temperatuurdaling mits history_data een geldige lijst is
        if isinstance(history_data, list) and len(history_data) > 0:
            temp_entries = history_data[0] if isinstance(history_data[0], list) else history_data
            if len(temp_entries) >= 2:
                try:
                    # Haal de states veilig op uit de dictionaries binnen de lijst
                    t_start = float(temp_entries[0].get("state"))
                    t_eind = float(temp_entries[-1].get("state"))
                    
                    totaal_verlies = t_start - t_eind
                    
                    # Logisch stilstandsverlies over 4 uur ligt tussen de 0 en 3 graden
                    if 0.0 < totaal_verlies < 3.0:
                        # 4 uur = 16 kwartieren
                        verlies_per_kwartier = totaal_verlies / 16
                        self.set_state("sensor.dhw_geleerd_kwartierverlies", state=str(round(verlies_per_kwartier, 4)))
                        self.log(f"DHW LEARN: Succesvol geleerd! Nieuw basisverlies is {round(verlies_per_kwartier, 4)}°C per kwartier.")
                    else:
                        self.log(f"DHW LEARN: Onrealistisch verlies gemeten ({round(totaal_verlies, 2)}°C). Nachtelijk verbruik? Afgewezen.")
                except (ValueError, TypeError, IndexError) as e:
                    self.log(f"DHW LEARN: Fout bij converteren van historische data naar getallen: {e}")

    def simuleer_dhw_behoefte(self, kwargs):
        # 1. Tarief configuratie
        marge_leverancier = 0.0205   
        energiebelasting = 0.09161    
        btw_percentage = 1.21         
        nordpool_schaalfactor = 1.0  

        comfort_min = 38.0  
        
        huidige_run_status = str(self.get_state("sensor.control_dhw_run_stop"))
        
        try:
            t_vat = float(self.get_state("sensor.dhw_current_temperature")) 
            zon_nu = float(self.get_state("sensor.solaredge_i1_ac_power", default=0))
            target_dhw = float(self.get_state("sensor.dhw_temperatuur_set_corrected"))
            fysieke_status = str(self.get_state("sensor.control_unit_operation_state_2"))
        except (ValueError, TypeError):
            self.log("DATA ERROR: Warmtepomp of target sensoren niet beschikbaar of niet-numeriek.")
            return

        # 2. Veilige conversie van het geleerde nachtverlies
        try:
            geleerd_verlies = self.get_state("sensor.dhw_geleerd_kwartierverlies")
            k_verlies_kwartier = float(geleerd_verlies) if geleerd_verlies else 0.035
        except (ValueError, TypeError):
            t_omgeving = 20.0  
            k_verlies_kwartier = max(0.01, (t_vat - t_omgeving) * 0.001)

        # 3. Dynamische stooktijd berekenen
        graden_per_kwartier_opwarmen = 3.0 
        temperatuur_te_overbruggen = max(1.0, target_dhw - t_vat)
        kwartieren_stooktijd = max(1, round(temperatuur_te_overbruggen / graden_per_kwartier_opwarmen))
        
        self.log(f"DHW LOGIC: Vat={t_vat}°C. Doel={target_dhw}°C. Kwartierverlies={round(k_verlies_kwartier, 4)}°C. Stooktijd={kwartieren_stooktijd} kwartier(en).")

        # --- DE BESLISSINGSMATRIX ---
        if t_vat >= target_dhw:
            self.log(f"DHW VOL: Vat ({t_vat}°C) is op target (>= {target_dhw}°C). Vrijgave UIT.")
            self.update_vrijgave_dhw(False)
            return

        if fysieke_status == "operation_state_dhw_on":
            if t_vat >= (target_dhw - 5.0): 
                self.log(f"DHW RUNNING: Warmtepomp stookt echt actief ({t_vat}°C). Cyclus afmaken. Vrijgave blijft AAN.")
                self.update_vrijgave_dhw(True)
                return

        if t_vat < comfort_min:
            self.log(f"DHW FORCEER: Vat is {t_vat}°C (onder comfortgrens). NU starten.")
            self.update_vrijgave_dhw(True)
            return

        if zon_nu > 1500:
            self.log(f"DHW ZON: Voldoende zonnestroom ({int(zon_nu)}W). Starten/Verlengen.")
            self.update_vrijgave_dhw(True)
            return

        nordpool_entiteit = "sensor.nordpool_huidige_kwartierprijs_nl"
        prices_today = self.get_state(nordpool_entiteit, attribute="prices_today") or []
        prices_tomorrow = self.get_state(nordpool_entiteit, attribute="prices_tomorrow") or []
        prijzen = prices_today + prices_tomorrow
        
        if not prijzen:
            self.log("DATA ERROR: Nordpool tarieven niet beschikbaar")
            return

        nu = self.get_now()          
        max_venster_kwartieren = 96  

        beschikbare_kwartieren = 0
        for q in range(0, max_venster_kwartieren + 1):
            test_tijd = nu + datetime.timedelta(minutes=q * 15)
            if self.get_forecast_value(prijzen, test_tijd, "price", 15) is None:
                if q > 4: 
                    beschikbare_kwartieren = q - 1
                    break
        
        actueel_venster = max(kwartieren_stooktijd, beschikbare_kwartieren if beschikbare_kwartieren > 0 else max_venster_kwartieren)

        # --- 3. DE SIMULATIE ---
        sim_t = t_vat
        noodzaak_vanaf_q = None
        laagste_prijs_sessie = 999.0
        moment_laagste_prijs = 0

        # Bereken de all-in prijs van NU (q=0) over de dynamische stooktijd
        totaal_prijs_nu = 0
        for s in range(0, kwartieren_stooktijd):
            tijd_s = nu + datetime.timedelta(minutes=s * 15)
            kaal_s = (self.get_forecast_value(prijzen, tijd_s, "price", 15) or 0.0) * nordpool_schaalfactor
            totaal_prijs_nu += (kaal_s + marge_leverancier + energiebelasting) * btw_percentage
        gem_prijs_nu = totaal_prijs_nu / kwartieren_stooktijd

        # Loop door het beschikbare venster per kwartier
        for q in range(0, actueel_venster - kwartieren_stooktijd + 1):
            sim_t = sim_t - k_verlies_kwartier
            
            if sim_t < comfort_min and noodzaak_vanaf_q is None:
                noodzaak_vanaf_q = q

            if noodzaak_vanaf_q is not None and q > noodzaak_vanaf_q:
                break

            totaal_prijs_sessie = 0
            for s in range(q, q + kwartieren_stooktijd):
                tijd_s = nu + datetime.timedelta(minutes=s * 15)
                kaal_s = (self.get_forecast_value(prijzen, tijd_s, "price", 15) or 0.0) * nordpool_schaalfactor
                totaal_prijs_sessie += (kaal_s + marge_leverancier + energiebelasting) * btw_percentage
            
            gem_prijs_sessie = totaal_prijs_sessie / kwartieren_stooktijd

            if gem_prijs_sessie < laagste_prijs_sessie:
                laagste_prijs_sessie = gem_prijs_sessie
                moment_laagste_prijs = q

        # --- 4. DE PRIJS-BESLISSING ---
        if t_vat < (target_dhw - 4.0): # Dynamische grens in plaats van vaste 47.0
            if gem_prijs_nu <= laagste_prijs_sessie + 0.005:
                self.log(f"DHW START: Prijs nu ({round(gem_prijs_nu,3)}) is optimaal.")
                self.update_vrijgave_dhw(True)
            elif noodzaak_vanaf_q is not None and noodzaak_vanaf_q <= 4:
                self.log(f"DHW CRITIEK: Binnen {noodzaak_vanaf_q*15}m leeg. Geforceerd starten.")
                self.update_vrijgave_dhw(True)
            else:
                self.log(f"DHW WACHT: Prijs nu ({round(gem_prijs_nu,3)}) > straks ({round(laagste_prijs_sessie,3)}) over {moment_laagste_prijs*15}m.")
                self.update_vrijgave_dhw(False)
        else:
            self.log(f"DHW STANDBY: Vat is nog {t_vat}°C. Geen actie vereist.")
            self.update_vrijgave_dhw(False)

    # --- 5. VEILIGE MODBUS COMMANDO'S ---
    def update_vrijgave_dhw(self, state):
        huidige_run_status = str(self.get_state("sensor.control_dhw_run_stop"))

        if state:
            # We zetten de warmtepomp alleen aan als hij nog niet loopt
            if huidige_run_status not in ["1", "Run", "on"]:
                self.log("Modbus actie: Zet warmtepomp AAN (vrijgave).")
                self.call_service("modbus/write_register", hub="atw-mbs-02", address=1024, value=1)
        else:
            # We zetten de warmtepomp alleen uit als hij nog niet gestopt is
            if huidige_run_status not in ["0", "Stop", "off"]:
                self.log("Modbus actie: Zet warmtepomp UIT (blokkeer).")
                self.call_service("modbus/write_register", hub="atw-mbs-02", address=1024, value=0)

    # --- 6. FORECAST HELPER ---
    def get_forecast_value(self, data_list, target_dt, key, window_minutes=15):
        for item in data_list:
            s_time = item.get('period_start') or item.get('start') or item.get('time')
            if not s_time: 
                continue
            if isinstance(s_time, str):
                try: 
                    s_time = datetime.datetime.fromisoformat(s_time)
                except: 
                    continue
            
            if s_time.tzinfo is not None and target_dt.tzinfo is None:
                target_dt = target_dt.replace(tzinfo=s_time.tzinfo)
            elif s_time.tzinfo is None and target_dt.tzinfo is not None:
                s_time = s_time.replace(tzinfo=target_dt.tzinfo)
                
            if s_time <= target_dt < (s_time + datetime.timedelta(minutes=window_minutes)):
                return float(item.get(key, 0))
        return None