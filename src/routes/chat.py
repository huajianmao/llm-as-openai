
from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from ..models import get_model
from ..models.llm import LlmModel
from ..type import ChatCompletionRequest, ChatCompletionResponse, ChatCompletionResponseChoice, ChatCompletionResponseStreamChoice, ChatMessage, DeltaMessage, UsageInfo
from ..utils.request import raise_if_invalid_model
from ..utils.function_call import need_function_call, build_chat_message


chat_router = APIRouter(prefix="/chat")


@chat_router.post("/completions", response_model=ChatCompletionResponse)
async def chat_completions(request: ChatCompletionRequest):
    if request.messages[-1].role != "user":
        raise HTTPException(status_code=400, detail="Invalid request format: last message must be from user")
    
    # FIXME:
    if request.functions is not None and request.model != "Qwen-7B-Chat":
        raise HTTPException(status_code=400, detail="Invalid request format: functions only supported by Qwen-7B-Chat")
    
    model = get_model(request.model)
    raise_if_invalid_model(model, LlmModel)
    kwargs = _gen_kwargs(request, model.tokenizer)

    stream = request.stream
    response, extra = model.chat(request.messages, functions=request.functions, stream=stream, **kwargs)
    # FIXME: finish_reason
    need = need_function_call(messages=request.messages, functions=request.functions)
    finish_reason = "function_call" if need else "stop"
    if request.stream:
        predict = _predict(model.id, response, extra)
        return EventSourceResponse(predict, media_type="text/event-stream")
    else:
        # compose function call response
        if need:
            message = build_chat_message(response)
        else:
            message=ChatMessage(role="assistant", content=response)
        choice_data = ChatCompletionResponseChoice( index=0, message=message, finish_reason=finish_reason)
        # FIXME: usage
        usage = UsageInfo()
        return ChatCompletionResponse(model=model.id, choices=[choice_data], object="chat.completion", usage=usage)


def _predict(model_id: str, generate, stream_type: str):
    yield _compose_chunk(model_id, DeltaMessage(role="assistant"))

    current_length = 0
    for response in generate:
        if stream_type == "delta":
            delta = response
            delta = delta[:-4] if delta.endswith("</s>") else delta
        else:
            if stream_type == "tuple":
                new_response, _ = response
            elif stream_type == "string":
                new_response = response

            if len(new_response) == current_length:
                continue
            delta = new_response[current_length:]
            current_length = len(new_response)
        yield _compose_chunk(model_id, DeltaMessage(content=delta))

    yield _compose_chunk(model_id, DeltaMessage())
    yield '[DONE]'


def _compose_chunk(model_id: str, message: DeltaMessage):
    choice_data = ChatCompletionResponseStreamChoice(
        index=0,
        delta=message,
        finish_reason="stop"
    )
    chunk = ChatCompletionResponse(model=model_id, choices=[choice_data], object="chat.completion.chunk")

    return "{}".format(chunk.json(exclude_unset=True, ensure_ascii=False))

def _gen_kwargs(request: ChatCompletionRequest, tokenizer):
    kwargs = {}
    # stop_words_ids
    if request.stop is not None:
        kwargs["stop_words_ids"] = [tokenizer.encode(word) for word in request.stop]

    return kwargs