# Contributing to LifeOS

Guidelines for contributing to LifeOS.

---

## Getting Started

### Prerequisites

- **Linux** (primary) or **macOS**
- **Python 3.11+**
- **Git**

macOS is only required for Apple Data Agent features (iMessage, Contacts, Photos). All other functionality works on both platforms.

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
   # Edit .env with your settings (LIFEOS_VAULT_PATH at minimum)
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
   git checkout -b feat/your-feature-name
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
   git commit -m "feat: description of what it does"
   ```

---

## Code Style

### Python

- Match existing style in the file you're editing
- Type hints encouraged but not required
- Keep changes minimal — don't reformat surrounding code

### Commit Messages

Format: `<type>: <imperative description>`

Types:
- `feat` — New feature
- `fix` — Bug fix
- `refactor` — Code restructuring (no behavior change)
- `docs` — Documentation only
- `test` — Adding or updating tests
- `chore` — Maintenance, CI, tooling

Examples:
- `feat: add calendar meeting prep endpoint`
- `fix: handle empty search results`
- `refactor: extract search logic to service`
- `docs: update installation guide for Linux`

### Branch Naming

Format: `<type>/<short-description>` — lowercase, hyphen-separated.

Examples: `feat/calendar-prep`, `fix/empty-search`, `docs/linux-setup`

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

---

## Pull Request Process

1. **Ensure tests pass**:
   ```bash
   ./scripts/test.sh smoke
   ```

2. **Update documentation** if needed

3. **Push your branch**:
   ```bash
   git push origin feat/your-feature-name
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
- Keep diffs under 400 lines (excluding tests and generated files)

### Include Tests

- New features need tests
- Bug fixes need regression tests
- Update existing tests if behavior changes

### Documentation

- Update docs for new features
- Add inline comments only for complex logic

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
├── scripts/             # CLI tools and service management
├── tests/               # Test suite
└── docs/                # Documentation
```

Key concepts:
- **Two-tier data model**: SourceEntity (raw) → PersonEntity (canonical)
- **Hybrid search**: Vector (semantic) + BM25 (keyword)
- **Entity resolution**: Links identifiers to canonical people
- **Service management**: systemd on Linux, launchd on macOS

See [Data & Sync](docs/specs/technical/data-and-sync.md) for details.

---

## Platform Notes

### Linux

- Primary development platform
- Services managed via systemd: `sudo ./scripts/setup-systemd.sh`
- GPU acceleration via ROCm (AMD) or CUDA (NVIDIA)

### macOS

- Required only for Apple Data Agent (iMessage, Contacts, Photos)
- Services managed via launchd: `./scripts/setup-launchd.sh`
- Full Disk Access needed for Apple data: see [Launchd Setup](docs/guides/launchd-setup.md)
- A Mac can act as a satellite, exporting Apple data nightly to a Linux server

---

## Getting Help

- **Questions**: Open a Discussion on GitHub
- **Bugs**: Open an Issue with reproduction steps
- **Features**: Open an Issue to discuss first

---

## License

By contributing, you agree that your contributions will be licensed under the GPL-3.0 License.
