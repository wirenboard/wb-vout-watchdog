def _disable_dnspython_doh():
    """
    Keep dnspython from importing httpx, which costs ~7 MB of RAM on armv7.

    paho-mqtt imports dns.resolver whenever dnspython is installed (only connect_srv() uses it;
    the service connects through a unix socket). dnspython >= 2.5 then imports httpx for
    DNS-over-HTTPS when python3-httpx is installed, and httpx imports rich, click and pygments
    for its command line tool. Nothing here uses DNS-over-HTTPS.

    dnspython decides it once, when dns.query is first imported, so this has to run before paho
    is imported; the package __init__ runs before any submodule, which guarantees that. The
    switch is process-wide. Older dnspython has no _features module and is left as is.
    Same approach as wb_common.mqtt_client.
    """
    try:
        # pylint: disable-next=import-outside-toplevel
        from dns import _features as dns_features

        dns_features.force("doh", False)
    except (ImportError, AttributeError):
        pass


_disable_dnspython_doh()
