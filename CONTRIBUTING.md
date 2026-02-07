# Contributing to LifeOS

Guidelines for contributing to LifeOS.

---

## Getting Started

### Prerequisites

- macOS (required for Apple integrations)
- Python 3.11+
- Git

### Setup

1. **Fork the repository** on GitHub

2. **Clone your fork**:
   ```bash
   git clone https://github.com/YOUR_USERNAME/LifeOS.git
   cd LifeOS
   ```

3. **Create virtual environment**:
   ```bash
   mkdir -p ~/.venvs
   python3 -m venv ~/.venvs/lifeos
   source ~/.venvs/lifeos/bin/activate
   pip install -r requirements.txt
   ```

4. **Set up environment**:
   ```bash
   cp .env.example .env
   # Edit .env with your settings
   ```

5. **Run tests**:
   ```bash
   ./scripts/test.sh
   ```

---

## Development Workflow

### Making Changes

1. Create a feature branch:
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. Make your changes

3. Restart server to test:
   ```bash
   ./scripts/server.sh restart
   ```

4. Run tests:
   ```bash
   ./scripts/test.sh smoke
   ```

5. Commit with a clear message:
   ```bash
   git commit -m "Add feature: description of what it does"
   ```

---

## Code Style

### Python

- **Formatter**: Black (default settings)
- **Import sorting**: isort
- **Type hints**: Encouraged but not required

Run formatters:
```bash
black .
isort .
```

### Commit Messages

Format: `<type>: <description>`

Types:
- `Add` - New feature
- `Fix` - Bug fix
- `Update` - Enhancement to existing feature
- `Refactor` - Code restructuring
- `Docs` - Documentation only
- `Test` - Adding or updating tests

Examples:
- `Add: calendar meeting prep endpoint`
- `Fix: handle empty search results`
- `Update: improve entity resolution scoring`
- `Refactor: extract search logic to service`

---

## Testing

### Running Tests

```bash
./scripts/test.sh          # Unit tests (~30s)
./scripts/test.sh smoke    # Unit + critical browser
./scripts/test.sh all      # Full suite
```

### Writing Tests

- Tests go in `tests/` directory
- Mirror the source file structure
- Use pytest fixtures for common setup
- Test both success and error cases

Example:
```python
# tests/test_services/test_search.py
import pytest
from api.services.search import search_vault

def test_search_returns_results():
    results = search_vault("test query")
    assert len(results) > 0

def test_search_handles_empty_query():
    results = search_vault("")
    assert results == []
```

---

## Pull Request Process

1. **Ensure tests pass**:
   ```bash
   ./scripts/test.sh smoke
   ```

2. **Update documentation** if needed

3. **Push your branch**:
   ```bash
   git push origin feature/your-feature-name
   ```

4. **Open a Pull Request** on GitHub

5. **Fill in the PR template**:
   - Summary of changes
   - Test plan
   - Screenshots (if UI changes)

6. **Address review feedback**

---

## Pull Request Guidelines

### Keep PRs Focused

- One feature or fix per PR
- Avoid unrelated changes
- Keep diffs minimal

### Include Tests

- New features need tests
- Bug fixes need regression tests
- Update existing tests if behavior changes

### Documentation

- Update docs for new features
- Update CHANGELOG if significant
- Add inline comments for complex logic

---

## Architecture

Before making changes, understand the structure:

```
LifeOS/
├── api/
│   ├── main.py          # FastAPI app
│   ├── routes/          # API endpoints
│   └── services/        # Business logic
├── config/              # Configuration
├── scripts/             # CLI tools
├── tests/               # Test suite
└── docs/                # Documentation
```

Key concepts:
- **Two-tier data model**: SourceEntity (raw) → PersonEntity (canonical)
- **Hybrid search**: Vector (semantic) + BM25 (keyword)
- **Entity resolution**: Links identifiers to canonical people

See [Data & Sync](docs/architecture/DATA-AND-SYNC.md) for details.

---

## Getting Help

- **Questions**: Open a Discussion on GitHub
- **Bugs**: Open an Issue with reproduction steps
- **Features**: Open an Issue to discuss first

---

## License

By contributing, you agree that your contributions will be licensed under the GPL-3.0 License.
