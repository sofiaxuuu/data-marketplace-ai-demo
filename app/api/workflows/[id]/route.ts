import { forwardToBackend } from "@/lib/backend";

type Context = { params: Promise<{ id: string }> };
export async function GET(request: Request, context: Context) {
  const { id } = await context.params;
  return forwardToBackend(`/workflows/${encodeURIComponent(id)}`, request);
}
export async function DELETE(request: Request, context: Context) {
  const { id } = await context.params;
  return forwardToBackend(`/workflows/${encodeURIComponent(id)}`, request);
}
