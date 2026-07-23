"""The AWS Bedrock provider boundary used by the Hermes runtime.

It owns the connection-bound region and ``bedrock-runtime`` apexes, accepts only
the fixed dummy access-key id, and re-signs allowed requests with the validated
operator credential.
"""
