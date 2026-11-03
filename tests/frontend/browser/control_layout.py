"""Check rendered field and action geometry without depending on CSS declarations."""

from itertools import pairwise

from playwright.sync_api import expect


def assert_field_actions_aligned(row, *, expected_buttons, expected_fields=1):
    """Keep actions level with their field or together below it when the row wraps."""
    fields = row.locator("label input, label select")
    buttons = row.locator(":scope > button")
    expect(fields).to_have_count(expected_fields)
    for field in fields.all():
        expect(field).to_be_visible()
    expect(buttons).to_have_count(expected_buttons)
    for button in buttons.all():
        expect(button).to_be_visible()
    field_boxes = [field.bounding_box() for field in fields.all()]
    button_boxes = [button.bounding_box() for button in buttons.all()]
    for button_box in button_boxes:
        assert button_box["height"] >= 44
        assert abs(button_box["y"] - button_boxes[0]["y"]) <= 1
        assert abs(button_box["height"] - button_boxes[0]["height"]) <= 1
    for field_box in field_boxes:
        if button_boxes[0]["y"] < field_box["y"] + field_box["height"]:
            assert abs(button_boxes[0]["y"] - field_box["y"]) <= 1
            assert abs(button_boxes[0]["height"] - field_box["height"]) <= 1
            assert field_box["x"] + field_box["width"] < button_boxes[0]["x"]
    for previous, following in pairwise(field_boxes + button_boxes):
        if abs(previous["y"] - following["y"]) <= 1:
            assert previous["x"] + previous["width"] < following["x"]
    assert row.page.evaluate("document.documentElement.scrollWidth <= innerWidth")
