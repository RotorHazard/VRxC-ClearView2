# RotorHazard VRx Control for ClearView 2.0

This system allows RotorHazard to communicate with ClearView 2.0 receivers (CV2) via the Clearview Comms Module (CVCM), sending race status messages, lap times, and split data in real time to the pilot's OSD. This system also provides race directors with module status information and will set the CV2's frequency from within the RotorHazard interface.

## Installation and Setup

The system is composed of a RotorHazard plugin and a hardware communicator.

### Install Plugin

RotorHazard 4.0 or later is required.

Copy the `vrx_cv2` plugin into the `src/server/plugins` directory in your RotorHazard install.

### Install Clearview API for Python

The [ClearView API](https://github.com/ryaniftron/clearview_interface_public.git) is required. On a typical linux setup, you may install with these commands:

```
# cd ~
# git clone https://github.com/ryaniftron/clearview_interface_public.git --depth 1
# cd ~/clearview_interface_public/src/clearview-py
# python2 -m pip install -e .
```

### Install MQTT

On the RotorHazard server or elsewhere on your network; install, configure, and run an MQTT server. A common option which available on many platforms is [Eclipse Mosquitto](https://mosquitto.org/). Configure your server to accept messages without authentication from the RotorHazard server.

### Configure Plugin

In RotorHazard's `config.json` file, add the following section.

```
"VRX_CONTROL": {
	"HOST": "localhost",
	"ENABLED": true
}
```
If you installed MQTT on a separate system than the RotorHazard server, replace the value of the `HOST` key with the domain or IP address of the MQTT server.

Only one server may use CV2 VRx Control on a given network at a time. Setting `ENABLED` to false is useful to store configuration settings when disabling a timer from VRx Control.

## Usage

Connect to the CVCM with the typical method. Enter the settings for the network where RotorHazard is running and switch the receiver to Station mode.

After attaching to the netowrk, each CV2 will appear in the conntected devices list. Use the controls on the `Settings` page to set each CV's seat number. The CV2 will receive messages for pilots on that seat.
