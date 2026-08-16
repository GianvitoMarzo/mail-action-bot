"""Adapters: everything that talks to the outside world.

Gmail, Bidoo and Telegram live here. The application core never imports this
package; the composition root (:mod:`bidoo_bot.container`) wires them together.
"""
