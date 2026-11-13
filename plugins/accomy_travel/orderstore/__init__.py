"""The bot's order store: bookings, their items and every supplier write, in PostgreSQL.

Modelled on hashtrip's order core. ``migrations/`` is the schema, ``db`` opens connections and runs
transactions, ``migrate`` applies the schema, ``store`` reads and writes orders. Nothing here calls a
supplier, and no tool uses the store yet: the bot is still search and pricing only.
"""
