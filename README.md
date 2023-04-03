# RotorHazard VRx Control for Clearview 2.0

This system allows RotorHazard to communicate with Clearview 2.0 receivers via the Clearview Comms Module (CVCM), sending race status messages, lap times, and split data in real time to the pilot's OSD. This system also provides race directors with module status information and will set the CV2's frequency from within the RotorHazard interface.

## Installation and Setup

The system is composed of a RotorHazard plugin and a hardware communicator.

### Install Plugin

RotorHazard 4.0 or later is required.

Copy the `vrx_cv2` plugin into the `src/server/plugins` directory in your RotorHazard install.


### Install the Clearview API for Python

```
# cd ~
# git clone https://github.com/ryaniftron/clearview_interface_public.git --depth 1
# cd ~/clearview_interface_public/src/clearview-py
# python2 -m pip install -e .
```

### Install MQTT

On the RH server or elsewhere on your network, install, configure, and run an MQTT server. A common option for this available on many platforms is [Eclipse Mosquitto](https://mosquitto.org/). Configure your server to accept messages without authentication from the RH server.

### Configure Plugin

In the `config.json` file, add the following section.

```
"VRX_CONTROL": {
	"HOST": "localhost",
	"ENABLED": true
}
```
Place the domain or IP address of the MQTT server in the `HOST` key.

Only one server may use CV2 VRx Control on a given network at a time. Setting ENABLED to false is useful to store configuration settings when disabling a timer from VRx Control.

If installation is successful, the RotorHazard log will contain the message `Loaded plugin module vrx_tbs` and `Importing VRx Controller tbs`.

## Usage

Connect to the CVCM with the typical method. Enter the settings for the network where RotorHazard is running and switch the receiver to Station mode.

After attaching to the netowrk, each ClearView 2.0 receiver will appear in the conntected devices list. Use the controls to set each CV's seat number. The ClearView 2.0 will receive messages for pilots on that seat.
