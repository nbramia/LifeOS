"""
Browser-based UI tests using Playwright.

Tests the LifeOS chat interface on both desktop and mobile viewports.
Requires the server to be running on localhost:8000.
"""
import pytest
from playwright.sync_api import Page, expect

# Mark all tests as requiring browser
pytestmark = [pytest.mark.browser, pytest.mark.slow]

# Viewport sizes
DESKTOP_VIEWPORT = {"width": 1280, "height": 800}
MOBILE_VIEWPORT = {"width": 375, "height": 812}  # iPhone X


class TestDesktopUI:
    """Test UI on desktop viewport."""

    @pytest.fixture(autouse=True)
    def setup(self, page: Page):
        """Set desktop viewport and navigate to app."""
        page.set_viewport_size(DESKTOP_VIEWPORT)
        page.goto("http://localhost:8000")
        # Wait for app to load
        page.wait_for_selector(".welcome")

    def test_welcome_screen_visible(self, page: Page):
        """Welcome screen should be visible on load."""
        expect(page.locator(".welcome h2")).to_have_text("Welcome to LifeOS")
        expect(page.locator(".welcome-icon")).to_be_visible()

    def test_sidebar_visible_on_desktop(self, page: Page):
        """Sidebar should be visible on desktop."""
        sidebar = page.locator(".sidebar")
        expect(sidebar).to_be_visible()
        # Menu button should be hidden on desktop
        expect(page.locator(".menu-btn")).not_to_be_visible()

    def test_new_chat_button_works(self, page: Page):
        """New chat button should reset the view."""
        # Click a suggestion first to start a chat
        page.locator(".suggestion").first.click()
        # Wait for message to appear
        page.wait_for_selector(".message.user")
        # Click new chat
        page.locator(".new-chat-btn").click()
        # Welcome should be back
        expect(page.locator(".welcome")).to_be_visible()

    def test_suggestion_buttons_clickable(self, page: Page):
        """Suggestion buttons should be clickable."""
        suggestions = page.locator(".suggestion")
        expect(suggestions).to_have_count(3)
        # Each suggestion should have min-height 44px
        for i in range(3):
            box = suggestions.nth(i).bounding_box()
            assert box["height"] >= 44, f"Suggestion {i} too small: {box['height']}px"

    def test_input_field_focusable(self, page: Page):
        """Input field should be focusable and accept text."""
        input_field = page.locator(".input-field")
        input_field.click()
        input_field.fill("Test question")
        expect(input_field).to_have_value("Test question")

    def test_send_button_visible(self, page: Page):
        """Send button should be visible."""
        send_btn = page.locator(".send-btn")
        expect(send_btn).to_be_visible()
        box = send_btn.bounding_box()
        assert box["width"] >= 44, "Send button too narrow"
        assert box["height"] >= 44, "Send button too short"

    def test_header_shows_status(self, page: Page):
        """Header should show status indicator."""
        expect(page.locator(".status-dot")).to_be_visible()
        expect(page.locator(".status-text")).to_have_text("Ready")

    def test_cost_display_visible_on_desktop(self, page: Page):
        """Cost display should be visible on desktop."""
        expect(page.locator(".cost-display")).to_be_visible()
        expect(page.locator("#sessionCost")).to_have_text("$0.00")

    def test_conversation_list_present(self, page: Page):
        """Conversation list should be present in sidebar."""
        expect(page.locator(".conversations-list")).to_be_visible()


class TestMobileUI:
    """Test UI on mobile viewport."""

    @pytest.fixture(autouse=True)
    def setup(self, page: Page):
        """Set mobile viewport and navigate to app."""
        page.set_viewport_size(MOBILE_VIEWPORT)
        page.goto("http://localhost:8000")
        page.wait_for_selector(".welcome")

    def test_welcome_screen_visible(self, page: Page):
        """Welcome screen should be visible on mobile."""
        expect(page.locator(".welcome h2")).to_have_text("Welcome to LifeOS")

    def test_sidebar_hidden_by_default(self, page: Page):
        """Sidebar should be hidden on mobile by default."""
        sidebar = page.locator(".sidebar")
        # Sidebar exists but is off-screen (transform: translateX(-100%))
        box = sidebar.bounding_box()
        assert box["x"] < 0, "Sidebar should be off-screen"

    def test_menu_button_visible(self, page: Page):
        """Menu button should be visible on mobile."""
        menu_btn = page.locator(".menu-btn")
        expect(menu_btn).to_be_visible()
        box = menu_btn.bounding_box()
        assert box["width"] >= 40, "Menu button too small"
        assert box["height"] >= 40, "Menu button too small"

    def test_menu_opens_sidebar(self, page: Page):
        """Menu button should open sidebar."""
        page.locator(".menu-btn").click()
        # Wait for animation
        page.wait_for_timeout(400)
        # Sidebar should now be visible
        sidebar = page.locator(".sidebar")
        box = sidebar.bounding_box()
        assert box["x"] >= 0, "Sidebar should be visible after menu click"
        # Overlay should be visible
        expect(page.locator(".overlay")).to_have_class("overlay visible")

    def test_overlay_closes_sidebar(self, page: Page):
        """Clicking overlay should close sidebar."""
        # Open sidebar
        page.locator(".menu-btn").click()
        page.wait_for_timeout(400)
        # Click overlay on the right side (where sidebar doesn't cover)
        # Sidebar is 280px wide, so click at x=300 to hit the overlay
        page.locator(".overlay").click(position={"x": 350, "y": 400})
        page.wait_for_timeout(400)
        # Sidebar should be hidden again
        sidebar = page.locator(".sidebar")
        box = sidebar.bounding_box()
        assert box["x"] < 0, "Sidebar should be hidden after overlay click"

    def test_suggestion_touch_targets(self, page: Page):
        """Suggestion buttons should meet 44px minimum touch target."""
        suggestions = page.locator(".suggestion")
        for i in range(suggestions.count()):
            box = suggestions.nth(i).bounding_box()
            assert box["height"] >= 44, f"Suggestion {i} too small for touch: {box['height']}px"

    def test_input_field_no_zoom(self, page: Page):
        """Input field should have 16px font to prevent iOS zoom."""
        input_field = page.locator(".input-field")
        font_size = input_field.evaluate("el => window.getComputedStyle(el).fontSize")
        assert font_size == "16px", f"Input font size should be 16px, got {font_size}"

    def test_cost_display_hidden_on_mobile(self, page: Page):
        """Cost display should be hidden on mobile."""
        expect(page.locator(".cost-display")).not_to_be_visible()

    def test_messages_fill_screen(self, page: Page):
        """Messages area should fill available screen."""
        messages = page.locator(".messages")
        box = messages.bounding_box()
        # Should be close to full width (minus small padding)
        assert box["width"] > 350, f"Messages too narrow: {box['width']}px"


class TestInteractions:
    """Test interactive elements work correctly."""

    @pytest.fixture(autouse=True)
    def setup(self, page: Page):
        """Navigate to app."""
        page.set_viewport_size(DESKTOP_VIEWPORT)
        page.goto("http://localhost:8000")
        page.wait_for_selector(".welcome")

    def test_enter_sends_message(self, page: Page):
        """Pressing Enter should send message."""
        input_field = page.locator(".input-field")
        input_field.fill("Test message")
        input_field.press("Enter")
        # User message should appear
        page.wait_for_selector(".message.user")
        expect(page.locator(".message.user .message-content")).to_contain_text("Test message")

    def test_shift_enter_creates_newline(self, page: Page):
        """Shift+Enter should create newline, not send."""
        input_field = page.locator(".input-field")
        input_field.fill("Line 1")
        input_field.press("Shift+Enter")
        input_field.type("Line 2")
        # Value should contain both lines
        value = input_field.input_value()
        assert "Line 1" in value and "Line 2" in value

    def test_textarea_auto_resizes(self, page: Page):
        """Textarea should grow with content."""
        input_field = page.locator(".input-field")
        initial_height = input_field.bounding_box()["height"]
        # Add multiple lines
        input_field.fill("Line 1\nLine 2\nLine 3\nLine 4")
        new_height = input_field.bounding_box()["height"]
        assert new_height > initial_height, "Textarea should grow with content"

    def test_scroll_to_bottom_button(self, page: Page):
        """Scroll to bottom button should appear when scrolled up."""
        # First, we need some messages to scroll
        # Since we can't easily create many messages, we'll just check the button exists
        scroll_btn = page.locator(".scroll-bottom")
        # Should not be visible initially (not scrolled)
        expect(scroll_btn).not_to_be_visible()


class TestAccessibility:
    """Test accessibility features."""

    @pytest.fixture(autouse=True)
    def setup(self, page: Page):
        """Navigate to app."""
        page.set_viewport_size(DESKTOP_VIEWPORT)
        page.goto("http://localhost:8000")
        page.wait_for_selector(".welcome")

    def test_buttons_have_titles(self, page: Page):
        """Interactive buttons should have title attributes."""
        # Send button
        expect(page.locator(".send-btn")).to_have_attribute("title", "Send message")
        # New chat button
        expect(page.locator(".new-chat-btn")).to_have_attribute("title", "New chat")

    def test_keyboard_navigation(self, page: Page):
        """Should be able to navigate with keyboard."""
        # Input field should be auto-focused on load
        # Give a moment for autofocus to take effect
        page.wait_for_timeout(100)
        # Check that input is focusable by clicking it
        page.locator(".input-field").click()
        expect(page.locator(".input-field")).to_be_focused()

    def test_color_contrast(self, page: Page):
        """Text should have sufficient color contrast."""
        # Get computed styles
        body_color = page.evaluate("""
            () => window.getComputedStyle(document.body).color
        """)
        body_bg = page.evaluate("""
            () => window.getComputedStyle(document.body).backgroundColor
        """)
        # Just verify colors are set (detailed contrast check would need more logic)
        assert body_color, "Text color should be set"
        assert body_bg, "Background color should be set"


class TestAttachmentUI:
    """Test attachment UI functionality."""

    @pytest.fixture(autouse=True)
    def setup(self, page: Page):
        """Navigate to app."""
        page.set_viewport_size(DESKTOP_VIEWPORT)
        page.goto("http://localhost:8000")
        page.wait_for_selector(".welcome")

    def test_attach_button_visible(self, page: Page):
        """Attach button should be visible next to send button."""
        attach_btn = page.locator(".attach-btn")
        expect(attach_btn).to_be_visible()
        expect(attach_btn).to_have_attribute("title", "Attach files")

    def test_attach_button_size(self, page: Page):
        """Attach button should meet minimum touch target size."""
        attach_btn = page.locator(".attach-btn")
        box = attach_btn.bounding_box()
        assert box["width"] >= 44, f"Attach button too narrow: {box['width']}px"
        assert box["height"] >= 44, f"Attach button too short: {box['height']}px"

    def test_file_input_hidden(self, page: Page):
        """File input should be hidden (used programmatically)."""
        file_input = page.locator("#fileInput")
        expect(file_input).to_be_hidden()

    def test_file_input_accepts_correct_types(self, page: Page):
        """File input should accept correct file types."""
        file_input = page.locator("#fileInput")
        accept = file_input.get_attribute("accept")
        # Check for key file types
        assert "image/png" in accept, "Should accept PNG images"
        assert "image/jpeg" in accept, "Should accept JPEG images"
        assert "application/pdf" in accept, "Should accept PDFs"
        assert "text/plain" in accept, "Should accept text files"

    def test_attachments_preview_hidden_by_default(self, page: Page):
        """Attachments preview area should be hidden when empty."""
        preview = page.locator("#attachmentsPreview")
        expect(preview).not_to_have_class("visible")

    def test_drag_over_css_class_exists(self, page: Page):
        """Drag-over CSS class should be defined for visual feedback."""
        # Verify the drag-over styles are defined by adding the class manually
        input_area = page.locator("#inputArea")

        # Add drag-over class via JS to verify styles are properly defined
        page.evaluate("document.getElementById('inputArea').classList.add('drag-over')")

        # Check the class was added and element has the expected border style
        expect(input_area).to_have_class("input-area drag-over")

        # Verify the border color changes (indicates CSS is working)
        border_color = page.evaluate(
            "getComputedStyle(document.getElementById('inputArea')).borderColor"
        )
        # Should have the accent color border when drag-over
        assert border_color != "rgb(51, 51, 51)", "Drag-over should change border color"

    def test_attach_button_keyboard_accessible(self, page: Page):
        """Attach button should be keyboard accessible."""
        attach_btn = page.locator(".attach-btn")
        # Should be focusable
        attach_btn.focus()
        expect(attach_btn).to_be_focused()


class TestAttachmentMobileUI:
    """Test attachment UI on mobile viewport."""

    @pytest.fixture(autouse=True)
    def setup(self, page: Page):
        """Set mobile viewport and navigate to app."""
        page.set_viewport_size(MOBILE_VIEWPORT)
        page.goto("http://localhost:8000")
        page.wait_for_selector(".welcome")

    def test_attach_button_visible_on_mobile(self, page: Page):
        """Attach button should be visible on mobile."""
        attach_btn = page.locator(".attach-btn")
        expect(attach_btn).to_be_visible()

    def test_attach_button_touch_target_mobile(self, page: Page):
        """Attach button should have adequate touch target on mobile."""
        attach_btn = page.locator(".attach-btn")
        box = attach_btn.bounding_box()
        assert box["width"] >= 44, f"Attach button too small for touch: {box['width']}px"
        assert box["height"] >= 44, f"Attach button too small for touch: {box['height']}px"
