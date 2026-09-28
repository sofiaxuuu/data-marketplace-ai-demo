import { forwardToBackend } from "@/lib/backend";

export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
  return forwardToBackend(`/conversations/${(await context.params).id}/resume-upload`, request);
}
