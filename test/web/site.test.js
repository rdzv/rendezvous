import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

test('Human/Agent switch copies the exact requested prompt, including backticks', async () => {
  const element = (dataset = {}) => ({ dataset, handlers: {}, attributes: {}, textContent: '',
    classList: { toggle() {} }, addEventListener(name, fn) { this.handlers[name] = fn; },
    setAttribute(name, value) { this.attributes[name] = value; } });
  const human = element({joinMode: 'human'}), agent = element({joinMode: 'agent'});
  const copy = element({copy: 'join-command'}), command = element(), status = element();
  let clipboard;
  const document = {
    querySelectorAll: selector => selector === '[data-join-mode]' ? [human, agent] : [copy],
    querySelector: () => copy,
    getElementById: id => id === 'join-command' ? command : status
  };
  vm.runInNewContext(fs.readFileSync(new URL('../../public/site.js', import.meta.url), 'utf8'),
    {document, navigator: {clipboard: {writeText: async text => { clipboard = text; }}}, setTimeout() {}});
  agent.handlers.click();
  await copy.handlers.click();
  assert.equal(clipboard, 'Join me on my terminal: `curl -fsSL https://join.rdzv.sh | sh -s -- -a --invitation "<invitation>"` with the following invitation:');
  assert.equal(agent.attributes['aria-pressed'], 'true');
  human.handlers.click();
  await copy.handlers.click();
  assert.equal(clipboard, 'curl -fsSL https://join.rdzv.sh | sh');
});
