"""Explicit collaborator base for the agent runtime."""


class ReviewerBackref:
    """Give a role collaborator access to the shared reviewer runtime."""

    def __init__(self, reviewer):
        self.reviewer = reviewer

    def __getattr__(self, name):
        return getattr(self.reviewer, name)
