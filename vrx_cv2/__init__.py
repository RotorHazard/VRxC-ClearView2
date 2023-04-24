'''CV2 VRx Control'''

# Sample configuration:
#     "VRX_CONTROL": {
#         "HOST": "localhost",
#         "ENABLED": true
#     }
#
# HOST domain or IP address of MQTT server for VRx Control messages
# ENABLED:true is required.
# ONLY ONE server may use VRx Control on a given network at a time. Setting ENABLED to false
# is useful to store configuration settings when disabling a timer from VRx Control.

# ClearView API
# cd ~
# git clone https://github.com/ryaniftron/clearview_interface_public.git --depth 1
# cd ~/clearview_interface_public/src/clearview-py
# python2 -m pip install -e .
import clearview  #pylint: disable=import-error

import json
import logging
import gevent
import traceback
from monotonic import monotonic

import Config

from .mqtt_topics import mqtt_publish_topics, mqtt_subscribe_topics, ESP_COMMANDS
from .VRxCV1_emulator import MQTT_Client
from eventmanager import Evt
import Results
from RHRace import WinCondition
import RHUtils

from VRxControl import VRxController, VRxDevice, VRxDeviceMethod

logger = logging.getLogger(__name__)

VRxALL = -1
MINIMUM_PAYLOAD = 7

def registerHandlers(args):
    if 'registerFn' in args:
        args['registerFn'](CV2Controller(
            'cv2',
            'ClearView 2.0'
        ))

def initialize(**kwargs):
    if 'Events' in kwargs:
        kwargs['Events'].on('VRxC_Initialize', 'VRx_register_CV2', registerHandlers, {}, 75, True)

class CV2Controller(VRxController):

    def validate_config(self, supplied_config):
        """Ensure config values are within range and reasonable values"""

        default_config = {
            'HOST': 'localhost',
        }
        saved_config = default_config

        for k, v_default in default_config.items():
            if k not in supplied_config:
                logger.warning("VRX Config does not include config key '%s'. Using '%s'"%(k, v_default))
            else:
                saved_config[k] = supplied_config[k]

        return saved_config

    def onStartup(self, args):
        logger.info("VRxController CV2 starting up")

        self.config = self.validate_config(Config.VRX_CONTROL)

        seat_frequencies = [node.frequency for node in self.nodes]

        # TODO the subscribe topics subscribe it to a seat number by default
        # Don't hack by making seat number a wildcard

        # TODO: pass in "CV1 to the MQTT_CLIENT because
        # there can be multiple clients, one for each protocol.
        # The MQTT_CLIENT should not know about what it is supposed to be doing
        # The VRxController can then run multiple clients, but duplicate messaging will have to be avoided
        # This could be done in the publisher by only passing messages to the clients that need it

        self._mqttc = MQTT_Client(client_id="VRxController",
                                 broker_ip=self.config["HOST"],
                                 subscribe_topics = None)

        self._add_subscribe_callbacks()
        self._mqttc.loop_start()
        self.num_seats = len(seat_frequencies)

        self.seat_number_range = (0,7)
        self._seats = [VRxSeat(self._mqttc, self.Language, n, seat_frequencies[n], seat_number_range=self.seat_number_range) for n in range(self.num_seats)]
        self._seat_broadcast = VRxBroadcastSeat(self._mqttc, self.Language)

        # Options
        if self.RHData.get_option('osd_lapHeader') is False:
            self.RHData.set_option('osd_lapHeader', 'L')
        if self.RHData.get_option('osd_positionHeader') is False:
            self.RHData.set_option('osd_positionHeader', '')


        self._seat_broadcast.reset_lock()
        # Request status of all receivers (static and variable)
        self.request_static_status()
        self.request_variable_status()
        self._seat_broadcast.turn_off_osd()

        for i in range(self.num_seats):
            self.get_seat_lock_status(i)
            gevent.spawn(self.set_seat_frequency, i, self._seats[i]._seat_frequency)

        # Update the DB with receivers that exist and their status
        # (Because the pi was already running, they should all be connected to the broker)
        # Even if the server.py is restarted, the broker continues to run:)

    def updateStatus(self):
        self.get_seat_lock_status()
        self.request_variable_status()

    def setDeviceSeat(self, device_id, seat):
        if seat is not None:
            self.set_seat_number(seat, None, device_id)
            super().setDeviceSeat(device_id, seat)
            self.setDeviceFrequency(device_id)
        else:
            logger.debug("Seat is {} for {}".format(seat, device_id))

    def setDeviceFrequency(self, device_id):
        seat_number = self.devices[device_id].map.seat
        if seat_number is not None:
            seatObj = self._seats[seat_number]
            frequency = seatObj.seat_frequency
            self.set_target_frequency(device_id, frequency)

    def onHeatSet(self, args):
        try:
            heat_id = args["heat_id"]
        except KeyError:
            logger.error("Unable to send callsigns. heat_id not found in event.")
            return

        for heatseat in self.RHData.get_heatNodes_by_heat(heat_id):
            heatseat_index = heatseat.node_index
            if heatseat_index < self.num_seats:  # TODO this may break with non-contiguous nodes
                if heatseat.pilot_id != RHUtils.PILOT_ID_NONE:
                    pilot = self.RHData.get_pilot(heatseat.pilot_id) 
                    self.set_message_direct(heatseat_index, pilot.callsign)
                else:
                    self.set_message_direct(heatseat_index, self.Language.__("-None-"))


    def onRaceStage(self, _args):
        self.set_message_direct(VRxALL, self.Language.__("Ready"))

    def onRaceStart(self, _args):
        self.set_message_direct(VRxALL, self.Language.__("Go"))

    def onRaceFinish(self, _args):
        self.set_message_direct(VRxALL, self.Language.__("Finish"))

    def onRaceStop(self, _args):
        self.set_message_direct(VRxALL, self.Language.__("Race Stopped. Land Now."))

    def onRaceLapRecorded(self, args):
        '''
        *** TODO: Formatting for hardware (OSD length, etc.)
        '''

        LAP_HEADER = self.RHData.get_option('osd_lapHeader')
        POS_HEADER = self.RHData.get_option('osd_positionHeader')

        if 'node_index' in args:
            seat_index = args['node_index']
        else:
            logger.warning('Failed to send results: Seat not specified')
            return False

        '''
        Get relevant results
        '''

        results = self.RACE.get_results(self.RHData)

        # select correct results
        # *** leaderboard = results[results['meta']['primary_leaderboard']]
        win_condition = self.RACE.format.win_condition

        if win_condition == WinCondition.FASTEST_3_CONSECUTIVE:
            leaderboard = results['by_consecutives']
        elif win_condition == WinCondition.FASTEST_LAP:
            leaderboard = results['by_fastest_lap']
        else:
            # WinCondition.MOST_LAPS
            # WinCondition.FIRST_TO_LAP_X
            # WinCondition.NONE
            leaderboard = results['by_race_time']

        # get this seat's results
        for index, result in enumerate(leaderboard):
            if result['node'] == seat_index: #TODO issue408
                rank_index = index
                break

        # check for best lap
        is_best_lap = False
        if result['fastest_lap_raw'] == result['last_lap_raw']:
            is_best_lap = True

        # get the next faster results
        next_rank_split = None
        next_rank_split_result = None
        if result['position'] > 1:
            next_rank_split_result = leaderboard[rank_index - 1]

            if next_rank_split_result['total_time_raw']:
                if win_condition == WinCondition.FASTEST_3_CONSECUTIVE:
                    if next_rank_split_result['consecutives_raw']:
                        next_rank_split = result['consecutives_raw'] - next_rank_split_result['consecutives_raw']
                elif win_condition == WinCondition.FASTEST_LAP:
                    if next_rank_split_result['fastest_lap_raw']:
                        next_rank_split = result['last_lap_raw'] - next_rank_split_result['fastest_lap_raw']
                else:
                    # WinCondition.MOST_LAPS
                    # WinCondition.FIRST_TO_LAP_X
                    next_rank_split = result['total_time_raw'] - next_rank_split_result['total_time_raw']
                    next_rank_split_fastest = ''
        else:
            # check split to self
            next_rank_split_result = leaderboard[rank_index]

            if win_condition == WinCondition.FASTEST_3_CONSECUTIVE or win_condition == WinCondition.FASTEST_LAP:
                if next_rank_split_result['fastest_lap_raw']:
                    if result['last_lap_raw'] > next_rank_split_result['fastest_lap_raw']:
                        next_rank_split = result['last_lap_raw'] - next_rank_split_result['fastest_lap_raw']

        # get the next slower results
        prev_rank_split = None
        prev_rank_split_result = None
        if rank_index + 1 in leaderboard:
            prev_rank_split_result = leaderboard[rank_index - 1]

            if prev_rank_split_result['total_time_raw']:
                if win_condition == WinCondition.FASTEST_3_CONSECUTIVE:
                    if prev_rank_split_result['consecutives_raw']:
                        prev_rank_split = result['consecutives_raw'] - prev_rank_split_result['consecutives_raw']
                        prev_rank_split_fastest = prev_rank_split
                elif win_condition == WinCondition.FASTEST_LAP:
                    if prev_rank_split_result['fastest_lap_raw']:
                        prev_rank_split = result['last_lap_raw'] - prev_rank_split_result['fastest_lap_raw']
                        prev_rank_split_fastest = result['fastest_lap_raw'] - prev_rank_split_result['fastest_lap_raw']
                else:
                    # WinCondition.MOST_LAPS
                    # WinCondition.FIRST_TO_LAP_X
                    prev_rank_split = result['total_time_raw'] - prev_rank_split_result['total_time_raw']
                    prev_rank_split_fastest = ''

        # get the fastest result
        first_rank_split = None
        first_rank_split_result = None
        if result['position'] > 2:
            first_rank_split_result = leaderboard[0]

            if next_rank_split_result['total_time_raw']:
                if win_condition == WinCondition.FASTEST_3_CONSECUTIVE:
                    if first_rank_split_result['consecutives_raw']:
                        first_rank_split = result['consecutives_raw'] - first_rank_split_result['consecutives_raw']
                elif win_condition == WinCondition.FASTEST_LAP:
                    if first_rank_split_result['fastest_lap_raw']:
                        first_rank_split = result['last_lap_raw'] - first_rank_split_result['fastest_lap_raw']
                else:
                    # WinCondition.MOST_LAPS
                    # WinCondition.FIRST_TO_LAP_X
                    first_rank_split = result['total_time_raw'] - first_rank_split_result['total_time_raw']

        '''
        Set up output objects
        '''

        osd = {
            'position_prefix': POS_HEADER,
            'position': str(result['position']),
            'callsign': result['callsign'],
            'lap_prefix': LAP_HEADER,
            'lap_number': '',
            'last_lap_time': '',
            'total_time': result['total_time'],
            'total_time_laps': result['total_time_laps'],
            'consecutives': result['consecutives'],
            'is_best_lap': is_best_lap,
        }

        if result['laps']:
            osd['lap_number'] = str(result['laps'])
            osd['last_lap_time'] = result['last_lap']
        else:
            osd['lap_prefix'] = ''
            osd['lap_number'] = self.Language.__('HS')
            osd['last_lap_time'] = result['total_time']
            osd['is_best_lap'] = False

        if next_rank_split:
            osd_next_split = {
                'position_prefix': POS_HEADER,
                'position': str(next_rank_split_result['position']),
                'callsign': next_rank_split_result['callsign'],
                'split_time': RHUtils.time_format(next_rank_split, self.RHData.get_option('timeFormat')),
            }

            osd_next_rank = {
                'position_prefix': POS_HEADER,
                'position': str(next_rank_split_result['position']),
                'callsign': next_rank_split_result['callsign'],
                'lap_prefix': LAP_HEADER,
                'lap_number': '',
                'last_lap_time': '',
                'total_time': result['total_time'],
            }

            if next_rank_split_result['laps']:
                osd_next_rank['lap_number'] = str(next_rank_split_result['laps'])
                osd_next_rank['last_lap_time'] = next_rank_split_result['last_lap']
            else:
                osd_next_rank['lap_prefix'] = ''
                osd_next_rank['lap_number'] = self.Language.__('HS')
                osd_next_rank['last_lap_time'] = next_rank_split_result['total_time']

        if first_rank_split:
            osd_first_split = {
                'position_prefix': POS_HEADER,
                'position': str(first_rank_split_result['position']),
                'callsign': first_rank_split_result['callsign'],
                'split_time': RHUtils.time_format(first_rank_split, self.RHData.get_option('timeFormat')),
            }

        '''
        Format and send messages
        '''

        # "Pos-Callsign L[n]|0:00:00"
        message = osd['position_prefix'] + osd['position'] + '-' + osd['callsign'][:10] + ' ' + osd['lap_prefix'] + osd['lap_number'] + '|' + osd['last_lap_time']

        if win_condition == WinCondition.FASTEST_3_CONSECUTIVE:
            # "Pos-Callsign L[n]|0:00:00 | #/0:00.000" (current | best consecutives)
            if result['laps'] >= 3:
                message += ' | 3/' + osd['consecutives']
            elif result['laps'] == 2:
                message += ' | 2/' + osd['total_time_laps']

        elif win_condition == WinCondition.FASTEST_LAP:
            if next_rank_split:
                # pilot in 2nd or lower
                # "Pos-Callsign L[n]|0:00:00 / +0:00.000 Callsign"
                message += ' / +' + osd_next_split['split_time'] + ' ' + osd_next_split['callsign'][:10]
            elif osd['is_best_lap']:
                # pilot in 1st and is best lap
                # "Pos:Callsign L[n]:0:00:00 / Best"
                message += ' / ' + self.Language.__('Best Lap')
        else:
            # WinCondition.MOST_LAPS
            # WinCondition.FIRST_TO_LAP_X
            # WinCondition.NONE

            # "Pos-Callsign L[n]|0:00:00 / +0:00.000 Callsign"
            if next_rank_split:
                message += ' / +' + osd_next_split['split_time'] + ' ' + osd_next_split['callsign'][:10]

        # send message to crosser
        seat_dest = seat_index
        self.set_message_direct(seat_dest, message)
        logger.debug('msg n{1}:  {0}'.format(message, seat_dest))

        # show split when next pilot crosses
        if next_rank_split:
            if win_condition == WinCondition.FASTEST_3_CONSECUTIVE or win_condition == WinCondition.FASTEST_LAP:
                # don't update
                pass

            else:
                # WinCondition.MOST_LAPS
                # WinCondition.FIRST_TO_LAP_X
                # WinCondition.NONE

                # update pilot ahead with split-behind

                # "Pos-Callsign L[n]|0:00:00"
                message = osd_next_rank['position_prefix'] + osd_next_rank['position'] + '-' + osd_next_rank['callsign'][:10] + ' ' + osd_next_rank['lap_prefix'] + osd_next_rank['lap_number'] + '|' + osd_next_rank['last_lap_time']

                # "Pos-Callsign L[n]|0:00:00 / -0:00.000 Callsign"
                message += ' / -' + osd_next_split['split_time'] + ' ' + osd['callsign'][:10]

                seat_dest = leaderboard[rank_index - 1]['node']
                self.set_message_direct(seat_dest, message)
                logger.debug('msg n{1}:  {0}'.format(message, seat_dest))

    def onLapsClear(self, args):
        self.set_message_direct(VRxALL, "---")

    def onFrequencySet(self, args):
        try:
            seat_index = args["nodeIndex"]
        except KeyError:
            logger.error("Unable to set frequency. nodeIndex not found in args")
            return
        try:
            frequency = args["frequency"]
        except KeyError:
            logger.error("Unable to set frequency. frequency not found in args")
            return

        self.set_seat_frequency(seat_index, frequency)

    def onSendPriorityMessage(self, args):
        logger.debug('VRx CV2 sendMessage')
        self.set_message_direct(VRxALL, args['message'])

    def onOptionSet(self, args):
        """Ensure config values are within range and reasonable values"""
        if 'option' in args:
            if args['option'] in ['osd_lapHeader', 'osd_positionHeader']:
                cv_csum = clearview.comspecs.clearview_specs["message_csum"]
                config_item = args['value']

                if len(config_item) == 1:
                    if config_item == cv_csum:
                        logger.error("Cannot use reserved character '%s' in '%s'"%(cv_csum, args['option']))
                        self.RHData.set_option(args['option'], '')
                elif cv_csum in config_item:
                    logger.error("Cannot use reserved character '%s' in '%s'"%(cv_csum, args['option']))
                    self.RHData.set_option(args['option'], '')

    def onShutdown(self, arg):
        logger.debug("VRx CV2 Shutting down")
        self._seat_broadcast.clear_user_message()
        self._seat_broadcast.turn_on_osd()
        self._seat_broadcast.set_wifi_state(clearview.comspecs.cv_device_limits["wifi_mode_ap"])

    ##############
    ## MQTT Status
    ##############

    def request_static_status(self, seat_number=VRxALL):
        if seat_number == VRxALL:
            seat = self._seat_broadcast
            seat.request_static_status()

            for device in self.devices:
                self.devices[device].last_request = monotonic()
        else:
            self._seats[seat_number].request_static_status()

            for device in self.devices:
                if self.devices[device].map.method == VRxDeviceMethod.SEAT and self.devices[device].map.seat == seat_number:
                    self.devices[device].last_request = monotonic()

    def request_variable_status(self, seat_number=VRxALL):
        if seat_number == VRxALL:
            seat = self._seat_broadcast
            seat.request_variable_status()

            for device in self.devices:
                self.devices[device].last_request = monotonic()
        else:
            self._seats[seat_number].request_variable_status()

            for device in self.devices:
                if self.devices[device].map.method == VRxDeviceMethod.SEAT and self.devices[device].map.seat == seat_number:
                    self.devices[device].last_request = monotonic()


    ##############
    ## Seat Number
    ##############

    def set_seat_number(self, desired_seat_num=None, current_seat_num=None, serial_num=None ):
        """Sets the seat subscription number to desired_number

        If targetting all devices at a certain seat, use 'current_seat_num'
        If targetting a single receiver serial number, use 'serial_num'
        If targetting all receivers, don't supply either 'current_seat_num' or 'serial_num'
        """
        MIN_SEAT_NUM = self.seat_number_range[0]
        MAX_SEAT_NUM = self.seat_number_range[1]
        desired_seat_num = int(desired_seat_num)
        if not MIN_SEAT_NUM <= desired_seat_num <= MAX_SEAT_NUM:
            return ValueError("Desired Seat Number %s out of range in set_seat_number"%desired_seat_num)

        if current_seat_num is not None:
            current_seat_num = int(current_seat_num)
            if not MIN_SEAT_NUM <= current_seat_num <= MAX_SEAT_NUM:
                return ValueError("Desired Seat Number %s out of range in set_seat_number"%current_seat_num)
            self._seats[current_seat_num].set_seat_number(desired_seat_num)
            return

        if serial_num is not None:
            topic = mqtt_publish_topics["cv1"]["receiver_command_esp_targeted_topic"][0]%serial_num
            cmd = json.dumps({"seat": str(desired_seat_num)})
            self._mqttc.publish(topic, cmd)
            self.devices[serial_num].extended_properties["needs_config"] = True
            return

        raise NotImplementedError("TODO Broadcast set all seat number")

    ###########
    # Frequency
    ###########

    def set_seat_frequency(self, seat_number, frequency):
        seat = self._seats[seat_number]
        seat.set_seat_frequency(frequency)

    def set_target_frequency(self, target, frequency):
        if frequency != RHUtils.FREQUENCY_ID_NONE:
            topic = mqtt_publish_topics["cv1"]["receiver_command_esp_targeted_topic"][0]%target

            # For ClearView, set the band and channel
            cv_bc = clearview.comspecs.frequency_to_bandchannel_dict(frequency)
            if cv_bc:
                self._mqttc.publish(topic, json.dumps(cv_bc))
            else:
                logger.warning("Unable to set ClearView frequency to %s", frequency)

            logger.debug("Set frequency for %s to %d", target, frequency)

    def get_seat_frequency(self, seat_number, frequency):
        self._seats[seat_number].seat_frequency

    #############
    # Lock Status
    #############

    # @property
    # def lock_status(self):
    #     self._lock_status = [seat.seat_lock_status for seat in self._seats]
    #     return self._lock_status

    def get_seat_lock_status(self, seat_number=VRxALL):
        if seat_number == VRxALL:
            seat = self._seat_broadcast
            seat.get_seat_lock_status()
        else:
            seat = self._seats[seat_number]
            seat.get_seat_lock_status()

        #return self._seats[seat_number].seat_lock_status

    #############
    # Camera Type
    #############

    @property
    def camera_type(self):
        self._camera_type = [seat.seat_camera_type for seat in self._seats]
        return self._camera_type

    @camera_type.setter
    def camera_type(self, camera_types):
        """ set the receiver camera types
        camera_types: dict
            key: seat_number
            value: desired camera_type in ['N','P','A']
        """
        for seat_index in camera_types:
            c = camera_types[seat_index]
            self._seats[seat_index].seat_camera_type = c

    def set_seat_camera_type(self, seat_number, camera_type):
        self._seats[seat_number].seat_camera_type = camera_type

    def get_seat_camera_type(self, seat_number, camera_type):
        self._seats[seat_number].seat_camera_type

    ##############
    # OSD Messages
    ##############

    def set_message_direct(self, seat_number, message):
        """set a message directly. Truncated if over length"""
        if message==None:
            logger.error("No message")
            return

        if seat_number == VRxALL:
            seat = self._seat_broadcast
            seat.set_message_direct(message)
        else:
            self._seats[seat_number].set_message_direct(message)

    #############################
    # Private Functions for MQTT
    #############################

    def _add_subscribe_callbacks(self):
        for rx_type in mqtt_subscribe_topics:
            topics = mqtt_subscribe_topics[rx_type]

            # All response
            topic_tuple = topics["receiver_response_all"]
            self._add_subscribe_callback(topic_tuple, self.on_message_resp_all)

            # Seat response
            topic_tuple = topics["receiver_response_seat"]
            self._add_subscribe_callback(topic_tuple, self.on_message_resp_seat)


            # Connection
            topic_tuple  = topics["receiver_connection"]
            self._add_subscribe_callback(topic_tuple, self.on_message_connection)

            # Targeted Response
            topic_tuple = topics["receiver_response_targeted"]
            self._add_subscribe_callback(topic_tuple, self.on_message_resp_targeted)

    def _add_subscribe_callback(self, topic_tuple, callback):
        formatter_name = topic_tuple[1]

        if formatter_name in ["#","+"]:   # subscibe to all at single level (+) or recursively all (#)
            topic = topic_tuple[0]%formatter_name
        elif formatter_name is None:
            topic = topic_tuple[0]
        elif isinstance(topic_tuple,tuple):
            raise ValueError("Uncaptured formatter_name: %s"%formatter_name)
        elif isinstance(topic_tuple,str):
            topic = topic_tuple
        else:
            raise TypeError("topic_tuple not of correct type: %s"%topic_tuple)

        self._mqttc.message_callback_add(topic, callback)
        self._mqttc.subscribe(topic)

    def perform_initial_receiver_config(self, target):
        """ Given the unique identifier of a receiver, perform the initial config"""
        initial_config_success = False

        try:
            _sn = self.devices[target].map.seat
        except KeyError:
            logger.info("No seat number available for %s yet", target)
        else:
            logger.info("Performing initial configuration for %s", target)

            seat_number = int(self.devices[target].map.seat)
            seat = self._seats[seat_number]
            frequency = seat.seat_frequency
            self.set_target_frequency(target, frequency)
            self.turn_off_osd_targeted(target)

            # TODO: send most relevant OSD information

            self.devices[target].extended_properties["needs_config"] = False
            initial_config_success = True

        return initial_config_success

    def on_message_connection(self, client, userdata, message):
        rx_name = message.topic.split('/')[1]

        if rx_name == 'VRxController':
            return

        connection_status = bool(message.payload == b'1')
        logger.info("Found MQTT device: %s => %s" % (rx_name,connection_status))

        device = VRxDevice()
        device.id = rx_name
        device.type = "ClearView 2.0"
        device.connected = connection_status

        self.addDevice(device)
        self.setDeviceMethod(rx_name, VRxDeviceMethod.SEAT)

        if device.connected:
            logger.info("Device %s is not yet configured by the server after a successful connection. Conducting some config now" % rx_name)
            self.devices[rx_name].extended_properties["needs_config"] = True

            # Start by requesting the status of the device that just joined.
            # At this point, it could be any MQTT device becaue we haven't filtered by receivers.
            # See TODO in on_message_status
            device.last_request = monotonic()
            self.req_status_targeted("variable", rx_name)
            self.req_status_targeted("static", rx_name)

        self.Events.trigger(Evt.VRX_DATA_RECEIVE, {
            'rx_name': rx_name,
            })

    def on_message_resp_all(self, client, userdata, message):
        payload = message.payload
        logger.info("TODO on_message_resp_all => %s"%(payload.strip()))

    def on_message_resp_seat(self, client, userdata, message):
        topic = message.topic
        seat_number = topic[-1]
        payload = message.payload
        logger.info("TODO on_message_resp_seat for seat %s => %s"%(seat_number, payload.strip()))

    def on_message_resp_targeted(self, client, userdata, message):
        topic = message.topic
        device_id = topic.split('/')[-1]
        device = self.devices[device_id]
        payload = message.payload
        if len(payload) >= MINIMUM_PAYLOAD:
            device.connected = True #TODO this is probably not needed
            device.last_response = monotonic()
            try:
                extracted_data = json.loads(payload)

            except:
                logger.warning("Can't load json data from '%s' of '%s'", device_id, payload)
                logger.debug(traceback.format_exc())
                device.ready = False
            else:
                device.ready = True

                # device.extended_properties.update(extracted_data)

                if "device_name" in extracted_data:
                    device.name = extracted_data["device_name"]

                if "ip_addr" in extracted_data:
                    device.address = extracted_data["ip_addr"]

                if "seat" in extracted_data and extracted_data["seat"].isnumeric():
                    device.map.seat = int(extracted_data["seat"])

                if "lock" in extracted_data:
                    rep_lock = extracted_data["lock"]

                    device.extended_properties["chosen_camera_type"] = rep_lock[0]
                    device.extended_properties["cam_forced_or_auto"] = rep_lock[1]
                    device.video_lock = rep_lock[2] == "L"

                if "video_format" in extracted_data:
                    device.extended_properties["video_format"] = extracted_data["video_format"]

                if "cv_version" in extracted_data:
                    device.extended_properties["cv_version"] = extracted_data["cv_version"]

                if "cvcm_version" in extracted_data:
                    device.extended_properties["cvcm_version"] = extracted_data["cvcm_version"]

                if "device_type" in extracted_data:
                    device.extended_properties["device_type"] = extracted_data["device_type"]

                if "osd_visibility" in extracted_data:
                    device.extended_properties["osd_visibility"] = extracted_data["osd_visibility"]

                #TODO only fire event if the data changed
                self.Events.trigger(Evt.VRX_DATA_RECEIVE, {
                    'device_id': device_id,
                    })

                if device.extended_properties["needs_config"] == True and device.ready == True:
                    self.perform_initial_receiver_config(device_id)


    def req_status_targeted(self, mode = "variable",serial_num = None):
        """Ask a targeted receiver for its status.
        Inputs:
            *mode: ["variable","static"]
            *serial_num: The devices's unique serial number to target it
        """

        if mode not in ["variable", "static"]:
            logger.error("Incorrect mode in req_status_targeted")
            return None
        if serial_num not in self.devices:
            logger.error("RX %s does not exist", serial_num)
            return None

        topic = mqtt_publish_topics["cv1"]["receiver_command_esp_targeted_topic"][0]%serial_num
        if mode == "variable":
            cmd = ESP_COMMANDS["Request Variable Status"]
        elif mode == "static":
            cmd = ESP_COMMANDS["Request Static Status"]
        else:
            raise Exception("Error checking mode has failed")
        self._mqttc.publish(topic,cmd)


    def turn_off_osd_targeted(self, target):
        """Turns off all OSD elements except user message"""
        topic = mqtt_publish_topics["cv1"]["receiver_command_esp_targeted_topic"][0]%target
        cmd = json.dumps({"osd_visibility" : "D"})
        self._mqttc.publish(topic, cmd)
        return cmd

    def turn_on_osd_targeted(self, target):
        """Turns on all OSD elements except user message"""
        topic = mqtt_publish_topics["cv1"]["receiver_command_esp_targeted_topic"][0]%target
        cmd = json.dumps({"osd_visibility" : "E"})
        self._mqttc.publish(topic, cmd)
        return cmd

CRED = '\033[91m'
CEND = '\033[0m'
def printc(*args):
    print(CRED + ' '.join(args) + CEND)

class BaseVRxSeat:
    """Seat controller for both the broadcast and individual seats"""
    def __init__(self,
                 mqtt_client, Language
                 ):

        self._mqttc = mqtt_client
        self.Language = Language
        logger = logging.getLogger(self.Language.__class__.__name__)

class VRxSeat(BaseVRxSeat):
    """Commands and Requests apply to all receivers at a seat number"""
    def __init__(self,
                 mqtt_client,
                 Language,
                 seat_number,
                 seat_frequency,
                 seat_number_range = (0,7), #(min,max)
                 seat_camera_type = 'A'
                 ):
        BaseVRxSeat.__init__(self, mqtt_client, Language)

        # RH refers to seats 0 to 7
        self.MIN_SEAT_NUM = seat_number_range[0]
        self.MAX_SEAT_NUM = seat_number_range[1]

        if self.MIN_SEAT_NUM <= seat_number <= self.MAX_SEAT_NUM:
            self._seat_number = seat_number
        elif seat_number == VRxALL:
            raise Exception("Use the broadcast seat")
        else:
            raise Exception("seat_number %d out of range", seat_number)

        self._seat_frequency = seat_frequency
        self._seat_camera_type = seat_camera_type
        self._seat_lock_status = None


        """
        osd_fields are used to format the OSD string with multiple pieces of data
        example fields names:
        * cur_lap_count
        * pilot_name
        * lap_time
        * split_time
        * total_laps
        The osd fields have the actual data (_osd_field_data),
        and the order (_osd_field_order)
        """
        self._osd_field_data = {}
        self._osd_field_order = {}

        # TODO specify the return value for commands.
        #   Do we return the command sent or some sort of result from mqtt?

    @property
    def seat_number(self):
        """Get the seat number"""
        logger.debug("seat property get")
        return self._seat_number

    @seat_number.setter
    def seat_number(self, seat_number):
        if self.MIN_SEAT_NUM <= seat_number <= self.MAX_SEAT_NUM:
            # TODO change the seat number of all receivers and apply the settings of the other seat number
            raise NotImplementedError
            # self._seat_number = seat_number
        else:
            raise Exception("seat_number out of range")

    def set_seat_number(self, new_seat_number):
        topic = mqtt_publish_topics["cv1"]["receiver_command_esp_seat_topic"][0]%self._seat_number
        cmd = json.dumps({"seat": str(new_seat_number)})
        self._mqttc.publish(topic, cmd)
        return

    @property
    def seat_frequency(self, ):
        """Gets the frequency of a seat"""
        return self._seat_frequency

    @seat_frequency.setter
    def seat_frequency(self, frequency):
        """Sets all receivers at this seat number to the new frequency"""
        raise NotImplementedError

    def set_seat_frequency(self, frequency):
        self.set_message_direct(self.Language.__("!!! Frequency changing to {0} in <10s !!!").format(frequency))
        gevent.sleep(10)

        self.set_seat_frequency_direct(frequency)
        self.set_message_direct(self.Language.__(""))

    def set_seat_frequency_direct(self, frequency):
        """Sets all receivers at this seat number to the new frequency"""
        self._seat_frequency = frequency
        if frequency != RHUtils.FREQUENCY_ID_NONE:

            # For ClearView, set the band and channel
            cv_bc = clearview.comspecs.frequency_to_bandchannel_dict(frequency)
            if cv_bc:
                topic = mqtt_publish_topics["cv1"]["receiver_command_esp_seat_topic"][0]%self._seat_number
                self._mqttc.publish(topic, json.dumps(cv_bc))

            else:
                logger.warning("Unable to set ClearView frequency to %s", frequency)

    @property
    def seat_camera_type(self, ):
        """Get the configured camera type for a seat number"""
        return self._seat_camera_type

    @seat_camera_type.setter
    def seat_camera_type(self, camera_type):
        if camera_type.capitalize in ["A","N","P"]:
            raise NotImplementedError
        else:
            raise Exception("camera_type out of range")

    @property
    def seat_lock_status(self, ):
        # topic = mqtt_publish_topics["cv1"]["receiver_request_seat_active_topic"][0]%self._seat_number
        # self._mqttc.publish(topic,
        #                    "?")
        # time.sleep(0.1)
        # return self._seat_lock_status
        pass
        print("TODO seat_lock_status property")

    def get_seat_lock_status(self,):
        topic = mqtt_publish_topics["cv1"]["receiver_command_esp_seat_topic"][0]%self._seat_number
        report_req = json.dumps({"lock": "?"})
        self._mqttc.publish(topic,report_req)
        return report_req

    def request_static_status(self):
        topic = mqtt_publish_topics["cv1"]["receiver_command_esp_seat_topic"][0]%self._seat_number
        msg = ESP_COMMANDS["Request Static Status"]
        self._mqttc.publish(topic,msg)

    def request_variable_status(self):
        topic = mqtt_publish_topics["cv1"]["receiver_command_esp_seat_topic"][0]%self._seat_number
        msg = ESP_COMMANDS["Request Variable Status"]
        self._mqttc.publish(topic,msg)

    def set_message_direct(self, message):
        """Send a raw message to the OSD"""
        topic = mqtt_publish_topics["cv1"]["receiver_command_esp_seat_topic"][0]%self._seat_number
        cmd = json.dumps({"user_msg" : message})
        self._mqttc.publish(topic, cmd)
        return cmd

    def turn_off_osd(self):
        """Turns off all OSD elements except user message"""
        topic = mqtt_publish_topics["cv1"]["receiver_command_esp_seat_topic"][0]%self._seat_number
        cmd = json.dumps({"osd_visibility" : "D"})
        self._mqttc.publish(topic, cmd)
        return cmd

    def turn_on_osd(self):
        """Turns on all OSD elements except user message"""
        topic = mqtt_publish_topics["cv1"]["receiver_command_esp_seat_topic"][0]%self._seat_number
        cmd = json.dumps({"osd_visibility" : "E"})
        self._mqttc.publish(topic, cmd)
        return cmd

    def _update_osd_by_fields(self):
        #todo
        # Get a list of keys sorted by field_order
        # concatenate the data togeter with a " " separator
        # send the OSD an update command

        #todo check against limit each addition and truncate based on field priority
        pass

class VRxBroadcastSeat(BaseVRxSeat):
    def __init__(self,
                 mqtt_client,
                 Language
                 ):
        BaseVRxSeat.__init__(self, mqtt_client, Language)
        self._cv_broadcast_id = clearview.comspecs.clearview_specs['bc_id']
        self._broadcast_cmd_topic = mqtt_publish_topics["cv1"]["receiver_command_all"][0]
        self._rx_cmd_esp_all_topic = mqtt_publish_topics["cv1"]["receiver_command_esp_all_topic"][0]

    def set_message_direct(self, message):
        """Send a raw message to all OSD's"""
        topic = self._rx_cmd_esp_all_topic
        cmd = json.dumps({"user_msg" : message})
        self._mqttc.publish(topic, cmd)
        return cmd

    def clear_user_message(self):
        """Clears the raw 'user message' on all OSD's"""
        topic = self._rx_cmd_esp_all_topic
        cmd = json.dumps({"user_msg" : ""}) # empty string
        self._mqttc.publish(topic, cmd)
        return cmd

    def turn_off_osd(self):
        """Turns off all OSD elements except user message"""
        topic = self._rx_cmd_esp_all_topic
        cmd = json.dumps({"osd_visibility" : "D"})
        self._mqttc.publish(topic, cmd)
        return cmd

    def turn_on_osd(self):
        """Turns on all OSD elements except user message"""
        topic = self._rx_cmd_esp_all_topic
        cmd = json.dumps({"osd_visibility" : "E"})
        self._mqttc.publish(topic, cmd)
        return cmd

    def reset_lock(self):
        """ Resets lock of all receivers"""
        topic = self._rx_cmd_esp_all_topic
        cmd = json.dumps({"lock": "1"})
        self._mqttc.publish(topic, cmd)
        return cmd

    def request_static_status(self):
        topic = self._rx_cmd_esp_all_topic
        cmd = ESP_COMMANDS["Request Static Status"]
        self._mqttc.publish(topic,cmd)

    def request_variable_status(self):
        topic = self._rx_cmd_esp_all_topic
        cmd = ESP_COMMANDS["Request Variable Status"]
        self._mqttc.publish(topic,cmd)

    def get_seat_lock_status(self,):
        topic = self._rx_cmd_esp_all_topic
        report_req = json.dumps({"lock":"?"})
        self._mqttc.publish(topic,report_req)
        return report_req

    def set_wifi_state(self, wifi_state):
        topic = self._rx_cmd_esp_all_topic
        cmd = json.dumps({"wifi": wifi_state})
        self._mqttc.publish(topic, cmd)
        return cmd

def main():
    # vrxc = VRxController("192.168.0.110",
    #                      [5740,
    #                       5760,
    #                       5780,
    #                       5800,
    #                       5820,
    #                       5840,
    #                       5860,
    #                       5880,])

    # # Set seat 3's frequency to 5781
    # vrxc.set_seat_frequency(3,5781)
    pass

if __name__ == "__main__":
    main()
