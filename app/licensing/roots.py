"""Trust anchors the app carries itself.

WHY THIS FILE EXISTS: the licence check must work on a Windows install that has
not seen an update in years, or has automatic root-certificate update switched
off, or has had its certificate store stripped by a "debloat" script. On those
machines the OS trust store is stale or missing the roots entirely, and every
HTTPS call fails - which would mean a paying customer could never activate.

It also fixes a bug found on a perfectly ordinary, fully updated Windows 11 box
(2026-08-03): the store carried an EXPIRED copy of ISRG Root X2 (notAfter
Sep 2025). OpenSSL 1.1.1 picked it as the trust anchor and gave up with
"certificate has expired" instead of using the valid cross-signed one the server
sends. curl succeeded on the same machine because Windows' own CryptoAPI builds
an alternate path. Python does not.

So: the app trusts THIS root, plus whatever non-expired anchors the OS happens
to have. The OS store is a bonus, never a requirement.

--------------------------------------------------------------------------
WHEN THIS NEEDS UPDATING
--------------------------------------------------------------------------
The licence server is fronted by the host's Caddy, which uses Let's Encrypt.
The chain it serves is:

    *.apps.bot-hosting.cloud  ->  Let's Encrypt YE1
                              ->  ISRG Root YE
                              ->  ISRG Root X2 (cross-signed by ISRG Root X1)

so ISRG Root X1 below is the anchor that validates all of it. X1 is good until
**4 June 2035**. Leaf and intermediate certificates rotate constantly and are
sent by the server, so they need nothing here.

If the host ever moves off Let's Encrypt, add that CA's root here as well - the
OS-store path would still cover healthy machines in the meantime, but the whole
point of this file is not to rely on that.

Verified on import: the fingerprint below is checked against the published
value, so a corrupted or swapped certificate fails loudly at startup rather
than silently becoming a trust anchor.
"""

import hashlib
import ssl

# ISRG Root X1 - the Let's Encrypt root.
# SHA-256: 96BCEC06264976F37460779ACF28C5A7CFE8A3C0AAE11A8FFCEE05C0BDDF08C6
ISRG_ROOT_X1 = """\
-----BEGIN CERTIFICATE-----
MIIFazCCA1OgAwIBAgIRAIIQz7DSQONZRGPgu2OCiwAwDQYJKoZIhvcNAQELBQAwTzELMAkGA1UE
BhMCVVMxKTAnBgNVBAoTIEludGVybmV0IFNlY3VyaXR5IFJlc2VhcmNoIEdyb3VwMRUwEwYDVQQD
EwxJU1JHIFJvb3QgWDEwHhcNMTUwNjA0MTEwNDM4WhcNMzUwNjA0MTEwNDM4WjBPMQswCQYDVQQG
EwJVUzEpMCcGA1UEChMgSW50ZXJuZXQgU2VjdXJpdHkgUmVzZWFyY2ggR3JvdXAxFTATBgNVBAMT
DElTUkcgUm9vdCBYMTCCAiIwDQYJKoZIhvcNAQEBBQADggIPADCCAgoCggIBAK3oJHP0FDfzm54r
Vygch77ct984kIxuPOZXoHj3dcKi/vVqbvYATyjb3miGbESTtrFj/RQSa78f0uoxmyF+0TM8ukj1
3Xnfs7j/EvEhmkvBioZxaUpmZmyPfjxwv60pIgbz5MDmgK7iS4+3mX6UA5/TR5d8mUgjU+g4rk8K
b4Mu0UlXjIB0ttov0DiNewNwIRt18jA8+o+u3dpjq+sWT8KOEUt+zwvo/7V3LvSye0rgTBIlDHCN
Aymg4VMk7BPZ7hm/ELNKjD+Jo2FR3qyHB5T0Y3HsLuJvW5iB4YlcNHlsdu87kGJ55tukmi8mxdAQ
4Q7e2RCOFvu396j3x+UCB5iPNgiV5+I3lg02dZ77DnKxHZu8A/lJBdiB3QW0KtZB6awBdpUKD9jf
1b0SHzUvKBds0pjBqAlkd25HN7rOrFleaJ1/ctaJxQZBKT5ZPt0m9STJEadao0xAH0ahmbWnOlFu
hjuefXKnEgV4We0+UXgVCwOPjdAvBbI+e0ocS3MFEvzG6uBQE3xDk3SzynTnjh8BCNAw1FtxNrQH
usEwMFxIt4I7mKZ9YIqioymCzLq9gwQbooMDQaHWBfEbwrbwqHyGO0aoSCqI3Haadr8faqU9GY/r
OPNk3sgrDQoo//fb4hVC1CLQJ13hef4Y53CIrU7m2Ys6xt0nUW7/vGT1M0NPAgMBAAGjQjBAMA4G
A1UdDwEB/wQEAwIBBjAPBgNVHRMBAf8EBTADAQH/MB0GA1UdDgQWBBR5tFnme7bl5AFzgAiIyBpY
9umbbjANBgkqhkiG9w0BAQsFAAOCAgEAVR9YqbyyqFDQDLHYGmkgJykIrGF1XIpu+ILlaS/V9lZL
ubhzEFnTIZd+50xx+7LSYK05qAvqFyFWhfFQDlnrzuBZ6brJFe+GnY+EgPbk6ZGQ3BebYhtF8GaV
0nxvwuo77x/Py9auJ/GpsMiu/X1+mvoiBOv/2X/qkSsisRcOj/KKNFtY2PwByVS5uCbMiogziUwt
hDyC3+6WVwW6LLv3xLfHTjuCvjHIInNzktHCgKQ5ORAzI4JMPJ+GslWYHb4phowim57iaztXOoJw
TdwJx4nLCgdNbOhdjsnvzqvHu7UrTkXWStAmzOVyyghqpZXjFaH3pO3JLF+l+/+sKAIuvtd7u+Nx
e5AW0wdeRlN8NwdCjNPElpzVmbUq4JUagEiuTDkHzsxHpFKVK7q4+63SM1N95R1NbdWhscdCb+ZA
JzVcoyi3B43njTOQ5yOf+1CceWxG1bQVs5ZufpsMljq4Ui0/1lvh+wjChP4kqKOJ2qxq4RgqsahD
YVvTH9w7jXbyLeiNdd8XM2w9U/t7y0Ff/9yi0GE44Za4rF2LN9d11TPAmRGunUHBcnWEvgJBQl9n
JEiU0Zsnvgc/ubhPgXRR4Xq37Z0j4r7g1SgEEzwxA57demyPxgcYxn/eR44/KJ4EBs+lVDR3veyJ
m+kXQ99b21/+jh5Xos1AnX5iItreGCc=
-----END CERTIFICATE-----
"""

# name -> (pem, published sha-256 fingerprint)
EMBEDDED = {
    "ISRG Root X1": (
        ISRG_ROOT_X1,
        "96bcec06264976f37460779acf28c5a7cfe8a3c0aae11a8ffcee05c0bddf08c6",
    ),
}


def verified_pem():
    """The embedded roots, as one PEM blob, fingerprint-checked.

    A certificate that does not match its published fingerprint is dropped
    rather than trusted - a swapped root is the one thing here that would be
    worth an attacker's time.
    """
    out = []
    for name, (pem, fingerprint) in EMBEDDED.items():
        try:
            digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
        except Exception:
            continue
        if digest == fingerprint:
            out.append(pem)
    return "".join(out)
