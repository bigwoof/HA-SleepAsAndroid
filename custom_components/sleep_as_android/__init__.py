"""Sleep As Android integration"""

import logging
from typing import Dict, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import entity_registry as er
from homeassistant.core import HomeAssistant, callback
from homeassistant.components.mqtt import subscription
from homeassistant.exceptions import NoEntitySpecifiedError

from .const import DOMAIN, DEVICE_MACRO
from .sensor import SleepAsAndroidSensor

_LOGGER = logging.getLogger(__name__)


async def async_setup(_hass: HomeAssistant, _config_entry: ConfigEntry):
    return True


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry):
    _LOGGER.info("Setting up %s ", config_entry.entry_id)

    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}

    registry = await er.async_get_registry(hass)
    hass.data[DOMAIN][config_entry.entry_id] = SleepAsAndroidInstance(hass, config_entry, registry)
    return True


class SleepAsAndroidInstance:
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, registry: er):
        self.hass = hass
        self._config_entry = config_entry
        self.__sensors: Dict[str, SleepAsAndroidSensor] = {}
        self._entity_registry: er = registry
        self._subscription_state = None

        try:
            self._name: str = self.get_from_config('name')
        except KeyError:
            self._name = 'SleepAsAndroid'

        try:
            _tt = self._config_entry.options['topic_template']
            self._updating = False
        except KeyError:
            self._updating = True

        # will call async_setup_entry from sensor.py
        self.hass.loop.create_task(self.hass.config_entries.async_forward_entry_setup(self._config_entry, 'sensor'))
        # ToDo prepare topic_template and other variables that should be defined one time.

    @property
    def device_position_in_topic(self) -> int:
        """ Position of DEVICE_MACRO in configured MQTT topic """
        result: int = 0

        for p in self.configured_topic.split('/'):
            if p == DEVICE_MACRO:
                break
            else:
                result += 1

        return result

    @staticmethod
    def device_name_from_topic_and_position(topic: str, position: int) -> str:
        """
        Get device name from full topic.
        :param topic: full topic from MQTT message
        :param position: position of device template

        :returns: device name
        """
        result: str = "unknown_device"
        s = topic.split('/')
        try:
            result = s[position]
        except KeyError:
            pass

        return result

    def device_name_from_topic(self, topic: str) -> str:
        """Get device name from topic

        :param topic: topic sting from MQTT message
        :returns: device name
        """
        return self.device_name_from_topic_and_position(topic, self.device_position_in_topic)

    @property
    def topic_template(self) -> str:
        """
        Converts topic with {device} to MQTT topic for subscribing
        """
        splitted = self.configured_topic.split('/')
        splitted[self.device_position_in_topic] = '+'
        return '/'.join(splitted)

    def get_from_config(self, name: str) -> str:
        try:
            data = self._config_entry.options[name]
        except KeyError:
            data = self._config_entry.data[name]

        return data

    @property
    def name(self) -> str:
        """Name of the integration in Home Assistant."""
        return self._name

    @property
    def configured_topic(self) -> str:
        """MQTT topic from integration configuration."""
        _topic = None

        try:
            _topic = self.get_from_config('topic_template')
        except KeyError:
            _topic = 'SleepAsAndroid/' + DEVICE_MACRO
            _LOGGER.warning("Could not find topic_template in configuration. Will use %s instead", _topic)

        return _topic

    def create_entity_id(self, device_name: str) -> str:
        """
        Generates entity_id based on instance name and device name.
        Used to identify individual sensors.

        :param device_name: name of device
        :returns: id that may be used for searching sensor by entity_id in entity_registry
        """
        _LOGGER.debug(f"create_entity_id: my name is {self.name}, device name is {device_name}")
        return self.name + "_" + device_name

    def device_name_from_entity_id(self, entity_id: str) -> str:
        """
        Extract device name from entity_id

        :param entity_id: entity id that was generated by self.create_entity_id
        :returns: pure device name
        """
        _LOGGER.debug(f"device_name_from_entity_id: entity_id='{entity_id}'")
        return entity_id.replace(self.name + "_", "", 1)

    @property
    def entity_registry(self) -> er:
        return self._entity_registry

    async def subscribe_root_topic(self, async_add_entities: Callable):
        """(Re)Subscribe to topics."""
        _LOGGER.debug("Subscribing to '%s' (generated from '%s')", self.topic_template, self.configured_topic)
        self._subscription_state = None

        @callback
        def message_received(msg):
            """Handle new MQTT messages."""

            _LOGGER.debug("Got message %s", msg)
            device_name = self.device_name_from_topic(msg.topic)
            entity_id = self.create_entity_id(device_name)
            _LOGGER.debug(f"sensor entity_id is {entity_id}")

            (target_sensor, is_new) = self.get_sensor(device_name)
            if is_new:
                async_add_entities([target_sensor], True)
            try:
                target_sensor.process_message(msg)
            except NoEntitySpecifiedError:
                # ToDo:  async_write_ha_state() runs before async_add_entities, so entity have no entity_id yet
                pass

        self._subscription_state = await subscription.async_subscribe_topics(
            self.hass,
            self._subscription_state,
            {
                "state_topic": {
                    "topic": self.topic_template,
                    "msg_callback": message_received,
                    "qos": self._config_entry.data['qos']
                }
            }
        )
        if self._subscription_state is not None:
            _LOGGER.debug("Subscribing to root topic is done!")
        else:
            _LOGGER.critical(f"Could not subscribe to topic {self.topic_template}")

    def get_sensor(self, sensor_name: str) -> (SleepAsAndroidSensor, bool):
        """
        Get sensor by it's name. If we have no such key in __sensors -- create new sensor
        :param sensor_name: name of sensor
        :return: (sensor with name "sensor_name", it it a new sensor)

        """
        try:
            return self.__sensors[sensor_name], False
        except KeyError:
            _LOGGER.info("New device! Let's create sensor for %s", sensor_name)
            new_sensor = SleepAsAndroidSensor(self.hass, self._config_entry, sensor_name)
            self.__sensors[sensor_name] = new_sensor
            return new_sensor, True

    @property
    def updating(self) -> bool:
        return self._updating

    def self_update(self):
        """
        Run updating routine
        """
        old_options = self._config_entry.options
        _LOGGER.debug(f"old options is {old_options}")
        try:
            new_options = {
                'name': old_options['name'],
                'qos': old_options['qos']
            }
        except KeyError:
            _LOGGER.debug("old options was not found. Will use defaults")
            new_options = {
                'name': self._name,
                'qos': 0,
            }
        try:
            topic = old_options['root_topic']
            new_topic = topic + "/" + DEVICE_MACRO
            _LOGGER.info(f"Found root_topic {topic} from v1.2.4. Will replace it by {new_topic}")
        except KeyError:
            _LOGGER.info(f"root_topic for v1.2.4 not found. Will try 'topic' from v1.1.0")
            try:
                topic = old_options['topic']  # full topic for message. Should cut after last /
                new_topic = '/'.join(topic.split('/')[:-1]) + "/" + DEVICE_MACRO
                _LOGGER.info(f"Found topic '{topic}' from v1.1.0. Will replace it by {new_topic}")
            except KeyError:
                new_topic = self.configured_topic
                _LOGGER.info(f"No topic information from previous versions found. Will use {new_topic}")

        new_options['topic_template'] = new_topic
        _LOGGER.info("Updating...")
        self.hass.config_entries.async_update_entry(
            self._config_entry,
            options=new_options
        )
        _LOGGER.info("Done!")
        self._updating = False
