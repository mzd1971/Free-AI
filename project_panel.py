"""Compatibility entry point for the former project window."""


def ProjectPanel(app):
    """Activate project mode inside the existing conversation."""
    app.open_project_assistant()
    return app.project
