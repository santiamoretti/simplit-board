"""SimplitSecurity board agent.

A small terminal tool that runs on the appliance. It registers the device under a friendly, generated name,
then connects to the presence relay to receive control's *signed* Java-service pushes — verifying each one
against control's public key before deploying and supervising the Java process. The Java service is the thing
that actually runs the security tooling; this agent only owns the update channel and the device identity.
"""

__version__ = "0.1.0"
