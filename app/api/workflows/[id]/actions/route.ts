import { forwardToBackend } from "@/lib/backend";

export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  return forwardToBackend(`/workflows/${encodeURIComponent(id)}/actions`, request);
}
