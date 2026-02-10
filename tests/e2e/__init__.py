"""
End-to-end tests for Telegram orchestration flows.

These tests verify the complete integration between:
- Intent classification (classify_action_intent)
- Chat API routes
- Reminder and Task CRUD operations
- Conversation context and disambiguation

Test Categories:
- test_telegram_intents.py: Intent classification accuracy
- test_telegram_multiselect.py: Numbered selection disambiguation
- test_telegram_tasks.py: Task CRUD via chat
- test_telegram_reminders.py: Reminder CRUD via chat
- test_telegram_code.py: Claude Code orchestration flows

Run: pytest tests/e2e/ -v
"""
