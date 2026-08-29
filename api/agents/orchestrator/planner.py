class WorkflowPlanner:
    """Turns a routed request type into an ordered agent plan.

    Ordering is not cosmetic. QA verifies the claims that Demo and Documentation
    make, so it has to run after them - scheduled first it would find nothing to
    check and report NO_CLAIMS. Release diffs the finished package against the
    previous crawl, so it runs last.
    """

    # Every plan starts by observing reality and structuring what was found.
    BASE = ["explorer", "knowledge_graph"]

    def create_plan(self, request_type: str) -> list:
        """Creates execution plan based on request type.

        Available agents: explorer, knowledge_graph, documentation, qa, demo, release.
        """
        req = (request_type or "").lower()

        if req == "qa":
            # Nothing to verify unless something produced claims first.
            return self.BASE + ["demo", "qa"]
        if req == "documentation":
            return self.BASE + ["documentation", "qa"]
        if req == "demo":
            return self.BASE + ["demo", "qa"]
        if req == "release":
            return self.BASE + ["release"]

        # Default full suite: produce, then verify, then diff.
        return self.BASE + ["documentation", "demo", "qa", "release"]
