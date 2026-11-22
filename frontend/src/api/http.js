export class ApiError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.status = status;
    this.detail = detail;
    this.contradictions = detail?.contradictions || null;
  }
}

export async function request(path, {token, body, download = false} = {}) {
  const headers = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(`/api${path}`, {
    method: body === undefined ? 'GET' : 'POST', headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const problem = await response.json().catch(() => ({}));
    const detail = typeof problem.detail === 'string' ? null : problem.detail;
    const message = typeof problem.detail === 'string' ? problem.detail
      : (problem.detail?.message || `请求失败 (${response.status})`);
    throw new ApiError(message, response.status, detail);
  }
  return download ? response.blob() : response.json();
}

export async function saveFile(path, token, filename) {
  const url = URL.createObjectURL(await request(path, {token, download: true}));
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}
