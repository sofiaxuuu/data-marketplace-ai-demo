import { forwardToBackend } from "@/lib/backend";

export async function GET(request: Request, context: { params: Promise<{ id: string }> }) {
  return forwardToBackend(`/conversations/${(await context.params).id}`, request);
}
export async function DELETE(request: Request, context: { params: Promise<{ id: string }> }) {
  return forwardToBackend(`/conversations/${(await context.params).id}${new URL(request.url).search}`, request);
}
