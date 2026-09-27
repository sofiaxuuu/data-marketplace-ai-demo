export async function forwardToBackend(path: string, request: Request): Promise<Response> {
  const base = process.env.DATASCOUT_API_URL ?? "http://127.0.0.1:8000";
  try {
    const upstream = await fetch(`${base}${path}`, {
      method: request.method,
      headers: { "content-type": "application/json" },
      body: request.method === "POST" ? await request.text() : undefined,
      cache: "no-store",
      signal: AbortSignal.timeout(180000),
    });
    return new Response(await upstream.text(), {
      status: upstream.status,
      headers: { "content-type": "application/json" },
    });
  } catch {
    return Response.json(
      { detail: "The DataScout API is unavailable. Start the Python service and try again." },
      { status: 502 },
    );
  }
}
