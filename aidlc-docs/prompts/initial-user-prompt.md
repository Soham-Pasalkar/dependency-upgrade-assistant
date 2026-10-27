# Manual User Prompt: Dependency Upgrade Assistant

## Your Role

You are an experienced Software Architect and Python Engineer specializing in AI-Driven Development Life Cycle (AI-DLC), software testing, and developer tooling.

Your task is to plan a small, working prototype called **Dependency Upgrade Assistant** for open-source contributors.

Follow a structured, human-approved development workflow. Do not begin implementation until I explicitly approve your plan.

## 1. Project Intent

Build a simple web application that helps open-source contributors identify outdated Python dependencies, inspect proposed dependency changes, and validate the supporting functionality before accepting the changes.

The application should demonstrate a practical development lifecycle: requirements analysis, planning, implementation, automated testing, and GitHub delivery.

## 2. Target Audience

Open-source contributors and maintainers who want to review dependency upgrades before incorporating them into their projects.

## 3. Proposed Technology Stack

- Language: Python
- Frontend: Streamlit
- Testing: pytest
- Version control: Git and GitHub
- Continuous integration: GitHub Actions
- Data source: A small, explicitly defined local dataset of package versions for the initial prototype.

Do not introduce an LLM API, database, authentication system, or live package registry integration unless you identify a compelling need and obtain my approval.

## 4. Core Features

1. Accept a user-provided `requirements.txt` file.
2. Parse package names and version constraints, including common formats such as `requests==2.28.0`.
3. Compare parsed dependencies against a predefined package-version dataset.
4. Identify packages for which a newer candidate version is available in that dataset.
5. Generate a proposed updated requirements file.
6. Display a readable unified code diff showing the original and proposed changes.
7. Allow the user to inspect the proposed changes before accepting them.
8. Provide a downloadable proposed requirements file.
9. Include automated pytest tests for dependency parsing, version comparison, and diff generation.
10. Include a README with setup instructions, usage instructions, limitations, and testing commands.
11. Include a GitHub Actions workflow that installs dependencies and runs pytest on pushes and pull requests.

Do not automatically modify arbitrary repositories, install proposed upgrades, or commit changes without explicit human approval.

## 5. Scope and Constraints

This is a time-constrained prototype. Prioritize a working, testable application over elaborate styling or additional features.

- Use deterministic version data so demonstrations are reproducible.
- Clearly distinguish a newer candidate version in the local dataset from a version verified to be the latest release.
- Handle invalid files, malformed version constraints, empty input, and unknown packages gracefully.
- Preserve dependencies that cannot be safely interpreted rather than silently deleting them.
- Use appropriate version-comparison logic rather than comparing version strings alphabetically.
- Keep the application modular, readable, and maintainable.
- Do not claim tests have passed unless they have actually been executed.
- Do not claim that the application has been deployed or pushed to GitHub unless that action has succeeded.

## 6. AI-DLC Planning Instructions

We are following a manual prompt-engineering workflow.

### Step A: Inspect the workspace

Inspect the existing project directory and identify available files, tools, and constraints. Do not overwrite existing work without approval.

### Step B: Clarify ambiguities

Identify decisions that materially affect the architecture, requirements, safety, or implementation.

Record unresolved questions in the plan file. Ask me for clarification rather than making critical decisions independently.

You may propose sensible defaults for minor decisions, but clearly label them as proposals.

### Step C: Create the implementation plan

Create a plan file under `plan/`.

Use a descriptive filename such as `plan/dependency-upgrade-assistant-plan.md`.

The plan must contain:

- Project objective and scope.
- Proposed architecture and technology stack.
- Implementation steps in execution order.
- Checkboxes for every step and meaningful substep.
- Testing and validation strategy.
- GitHub repository and CI delivery steps.
- Risks, assumptions, and unresolved questions.
- Acceptance criteria.
- A list of files expected to be created or modified.

### Step D: Create the requirements document

Create `aidlc-docs/requirements/dependency-upgrade-assistant-requirements.md`.

Include:

- Problem statement.
- Target users.
- Functional requirements with unique IDs.
- Non-functional requirements with unique IDs.
- Input validation and error-handling requirements.
- Security considerations.
- Testing requirements.
- Acceptance criteria that can be objectively verified.
- Explicit out-of-scope features.

### Step E: Record the prompt and approval history

Create an `aidlc-docs/` directory for development records.

Save this initial user prompt in `aidlc-docs/prompts/initial-user-prompt.md`.

Create an approval log at `aidlc-docs/approvals/approval-log.md`. Record approval decisions and timestamps when they actually occur. Do not fabricate approvals or timestamps.

### Step F: Request my approval

After completing the planning and requirements documents:

1. Summarize the proposed architecture.
2. Summarize the requirements and acceptance criteria.
3. List any unresolved questions.
4. Show the location of each generated document.
5. Ask me to review and approve the plan before implementation.

## 7. Strict Execution Boundary

**For this prompt, perform planning and documentation only.**

Do not implement application features, generate the application source code, run deployment operations, create commits, or push to GitHub.

Stop after producing the plan and requirements documents and requesting my approval.

Once I explicitly approve the plan, I will provide the next prompt authorizing implementation.
