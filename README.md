# Thermostat Schedule Controller

A Python script that automatically configures smart zigbee thermostats with daily temperature schedules using MQTT communication to zigbee2mqtt. The script supports multiple thermostat types and publishes JSON payloads with optimized temperature scheduling.

## 📋 Project Overview

This script was designed to solve the challenge of managing multiple smart thermostats with different communication protocols.

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
