# Alert Discord Monitor(Minecraft)

A flexible, Python-based monitoring script that polls API endpoints and triggers Discord alerts when specific conditions are met. 

Configured entirely via YAML, this tool allows you to set up custom logic to track data changes over time. It is currently configured to monitor a Minecraft server (via mcsrvstat.us), automatically notifying a Discord channel when the server goes offline or when the player count reaches a certain threshold.

## Key Features
* **YAML Configuration:** Easily add new APIs, set polling intervals, and define alert rules without touching the Python code.
* **Custom Alert Rules:** Trigger alerts based on strict thresholds (e.g., player count > 10) or state changes (e.g., server status changes from online to offline).
* **Minecraft Integration:** Out-of-the-box support for tracking Java and Bedrock servers hosted on platforms like Aternos.
* **Automated Discord Notifications:** Sends clean, formatted alerts directly to your Discord server, including "resolved" notifications when things go back to normal.

