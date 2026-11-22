"""Autonomous cleanup of agent workspaces.

The package has two command-line tools:

* ``agent-ledger`` records which agent owns which clone and resources.
* ``agent-sweep`` applies fixed rules to every clone and ledger entry: it removes
  rebuildable build outputs, rescues unsafe work, deletes finished clones and
  removes recorded resources. It never asks anything; unclear cases are kept.
"""

__version__ = "0.1.0"
