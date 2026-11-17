"""Read hourly chart evidence through the supported keyboard interaction."""


def show_hour(page, index=0):
    """Open an independently chosen hour without invoking chart callbacks as the user action."""
    canvas = page.locator("#quality-chart-0")
    canvas.focus()
    canvas.press("Home")
    for _ in range(index):
        canvas.press("ArrowRight")
    return page.get_by_role("region", name="Hourly forecast evidence", exact=True)
