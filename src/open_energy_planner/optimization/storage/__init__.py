"""Persist the requested planning objective without rewriting archived plan strategies."""

from open_energy_planner.optimization.domain import Strategy


class StrategyStore:
    """Keep the requested objective in the active database. Plans retain their own immutable choice."""

    def __init__(self, documents):
        """Restore a supported choice, with an explicit fault for unreadable settings."""
        self.documents = documents
        self.current, self.issue = Strategy.COST, None
        try:
            if documents.exists("current.json"):
                self.current = Strategy(documents.read("current.json"))
        except (ValueError, TypeError, OSError):
            self.issue = "strategy_configuration_unreadable"

    def save(self, strategy):
        """Commit before changing live intent. Failed writes leave the old request intact."""
        strategy = Strategy(strategy)
        self.documents.write("current.json", strategy.value)
        self.current, self.issue = strategy, None
