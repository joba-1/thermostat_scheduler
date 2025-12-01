# Thermostat Schedule Controller

A Python script that automatically configures smart zigbee thermostats with daily temperature schedules using MQTT communication to zigbee2mqtt. The script supports multiple thermostat types and publishes JSON payloads with optimized temperature scheduling.

## 📋 Project Overview

This script was designed to solve the challenge of managing multiple smart thermostats with different communication protocols.

## 🎯 Original Prompt

> Write a python script to change thermostat settings to follow daily temperature schedules by publishing json payloads with mqtt. There are different thermostat types. Each type uses different json keys and values to follow the defined schedules. The script shall start with some definitions: mqtt broker name, mqtt base topic, dictionary of all thermostats consisting of thermostat name, day hour, day temperature, night hour, night temperature and type of thermostat and a dictionary of thermostat types defining the keys and values to go to scheduling mode. For each thermostat construct a schedule string of 6 time and temperature pairs of the format hh:mm/tt. 2 pairs from night to day and 4 from day to night with equal intervals each. configure a thermostat by publishing to topic constructed from the base topic the thermostat name and "set". The payload json contains the keys and values of the thermostat type to bring it into schedule mode and one key for each weekday named "schedule_{weekday}" with the schedule string as value.

## ✨ Key Features

### 🏠 **Multi-Thermostat Support**
- Manages multiple thermostats simultaneously
- Each thermostat can have unique day/night temperature preferences
- Supports different thermostat brands

### 🔧 **Thermostat Type Abstraction**
- **Type-Specific Configuration**: Each thermostat brand uses different JSON keys
- **Schedule Mode Activation**: Automatically enables scheduling mode per device type
- **Extensible Design**: Easy to add new thermostat types

### 📡 **MQTT Communication**
- **Reliable Publishing**: Uses QoS 1 for guaranteed delivery
- **Structured Topics**: `{base_topic}/{thermostat_name}/set`

### 📊 **Weekly Schedule Management**
- **7-Day Coverage**: Identical schedules for all weekdays

### 🛡️ **Error Handling & Logging**
- **Connection Monitoring**: MQTT broker connection status
- **Publish Confirmation**: Success/failure feedback for each thermostat
- **Type Validation**: Checks for unknown thermostat types
- **Graceful Cleanup**: Proper disconnection and resource management

## 🚀 Installation

### Prerequisites
```bash
pip install paho-mqtt
