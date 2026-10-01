"""The link between Miki's brain (the always-on server) and her hands (your laptop).

The server runs everything that thinks and remembers: chat, memory, mail, calendar, the phone bot, the focus clock.
The laptop only does what needs a real screen: the dashboard window, and focus mode's windows, bouncer and pet.

    laptop                                      server (127.0.0.1 only)
    hands agent ── ssh -L 18765 ─────────────▶  LinkHub :8765  ──▶  WebApi / FocusService / phone bot
    dashboard window ──▶ 127.0.0.1:18765 ──┘

The hub listens on the server's loopback only, so nothing new is exposed to the internet: the laptop reaches it through
an SSH tunnel (the same key you already use to log in), and every connection must also prove it knows MIKI_LINK_TOKEN.
"""
