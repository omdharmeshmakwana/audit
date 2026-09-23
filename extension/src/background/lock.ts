// Serialise chrome.storage read-modify-write sections so concurrent messages don't lose
// writes. Never await network I/O inside `locked`, and never nest `locked` calls.

let chain: Promise<unknown> = Promise.resolve();

export function locked<T>(fn: () => Promise<T>): Promise<T> {
  const next = chain.then(fn, fn);
  chain = next.catch(() => undefined);
  return next;
}
