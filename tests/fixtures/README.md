# Test Fixtures

This directory contains test data fixtures.

## Generic Fixtures (Committed)

- `example_test_data.py` - Template with placeholder data

## Production Fixtures (Gitignored)

- `production_test_data.py` - Your real names/domains for local testing

## Setting Up Production Test Data

To test with your actual production config:

1. Copy `example_test_data.py` to `production_test_data.py`
2. Replace placeholders with real values
3. Tests will automatically load production data if the file exists

```bash
cp tests/fixtures/example_test_data.py tests/fixtures/production_test_data.py
# Edit production_test_data.py with your values
```

The `conftest.py` fixture `production_test_data` will return `None` if the
production file doesn't exist, allowing tests to use generic data instead.
