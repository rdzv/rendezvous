const agentPrompt = 'Join me on my terminal: `curl -fsSL https://join.rdzv.sh | sh -s -- -a --invitation "<invitation>"` with the following invitation:';
for (const button of document.querySelectorAll('[data-join-mode]')) {
  button.addEventListener('click', () => {
    const agent = button.dataset.joinMode === 'agent';
    const command = document.getElementById('join-command');
    command.textContent = agent ? agentPrompt : 'curl -fsSL https://join.rdzv.sh | sh';
    command.classList.toggle('agent-prompt', agent);
    for (const choice of document.querySelectorAll('[data-join-mode]')) {
      choice.setAttribute('aria-pressed', String(choice === button));
    }
    document.querySelector('[data-copy="join-command"]').setAttribute('aria-label', agent ? 'Copy agent prompt' : 'Copy join command');
  });
}

for (const button of document.querySelectorAll('[data-copy]')) {
  button.addEventListener('click', async () => {
    const source = document.getElementById(button.dataset.copy);
    const status = document.getElementById('copy-status');
    try {
      await navigator.clipboard.writeText(source.textContent);
      status.textContent = 'Command copied.';
      button.setAttribute('aria-label', 'Copied');
      setTimeout(() => button.setAttribute('aria-label', 'Copy command'), 2000);
    } catch {
      status.textContent = 'Select and copy the command manually.';
    }
  });
}
