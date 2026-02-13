DOMAIN = "hitachi_yutaki_cop"

# Sensor names
SENSOR_INDOOR_POWER = "sensor.shelly_warmtepomp_binnenunit_active_power"
SENSOR_OUTDOOR_POWER = "sensor.shelly_warmtepomp_buitenunit_active_power"
SENSOR_DHW_HEATER = "binary_sensor.control_unit_dhw_heater_2"
SENSOR_OUTLET_TEMP = "sensor.control_unit_water_outlet_temperature_2"
SENSOR_INLET_TEMP = "sensor.control_unit_water_inlet_temperature_2"
SENSOR_FLOW = "sensor.control_unit_water_flow_2"
SENSOR_OPERATION_STATE = "sensor.control_unit_operation_state_2"
SENSOR_DHW_CURRENT_TEMP = "sensor.dhw_current_temperature"
SENSOR_DHW_TARGET_TEMP = "sensor.dhw_temperatuur_set_corrected"

# Operation states
STATE_HEAT_THERMO = "operation_state_heat_thermo_on"
STATE_HEAT_COOL = "operation_state_heat_cool_on"
STATE_DHW = "operation_state_dhw_on"

# Default values
DEFAULT_PUMP_POWER = 59
DEFAULT_DHW_TANK_VOLUME = 260

# Attributes
ATTR_PUMP_POWER = "pump_power"
ATTR_DHW_TANK_VOLUME = "dhw_tank_volume"
