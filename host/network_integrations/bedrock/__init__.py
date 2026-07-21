"""The AWS Bedrock provider boundary shared by the Pi and Hermes runtimes.

It owns the connection-bound region and ``bedrock-runtime`` apexes, accepts only
the fixed dummy access-key id, and re-signs allowed requests with the validated
operator credential.
"""
