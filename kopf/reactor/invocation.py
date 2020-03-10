"""
Invoking the callbacks, including the args/kwargs preparation.

Both sync & async functions are supported, so as their partials.
Also, decorated wrappers and lambdas are recognized.
All of this goes via the same invocation logic and protocol.
"""
import asyncio
import contextlib
import contextvars
import functools
from typing import Optional, Any, Union, List, Iterable, Iterator, Tuple, Dict, cast, TYPE_CHECKING

from kopf import config
from kopf.reactor import callbacks
from kopf.reactor import causation
from kopf.structs import dicts

if TYPE_CHECKING:
    asyncio_Future = asyncio.Future[Any]
else:
    asyncio_Future = asyncio.Future

Invokable = Union[
    callbacks.ActivityHandlerFn,
    callbacks.ResourceHandlerFn,
]


@contextlib.contextmanager
def context(
        values: Iterable[Tuple[contextvars.ContextVar[Any], Any]],
) -> Iterator[None]:
    """
    A context manager to set the context variables temporarily.
    """
    tokens: List[Tuple[contextvars.ContextVar[Any], contextvars.Token[Any]]] = []
    try:
        for var, val in values:
            token = var.set(val)
            tokens.append((var, token))
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def build_kwargs(
        cause: Optional[causation.BaseCause] = None,
        **kwargs: Any
) -> Dict[str, Any]:
    """
    Expand kwargs dict with fields from the causation.
    """
    new_kwargs = {}
    new_kwargs.update(kwargs)

    # Add aliases for the kwargs, directly linked to the body, or to the assumed defaults.
    if isinstance(cause, causation.BaseCause):
        new_kwargs.update(
            cause=cause,
            logger=cause.logger,
        )
    if isinstance(cause, causation.ActivityCause):
        new_kwargs.update(
            activity=cause.activity,
        )
    if isinstance(cause, causation.ResourceCause):
        new_kwargs.update(
            patch=cause.patch,
            memo=cause.memo,
            body=cause.body,
            spec=dicts.DictView(cause.body, 'spec'),
            meta=dicts.DictView(cause.body, 'metadata'),
            status=dicts.DictView(cause.body, 'status'),
            uid=cause.body.get('metadata', {}).get('uid'),
            name=cause.body.get('metadata', {}).get('name'),
            namespace=cause.body.get('metadata', {}).get('namespace'),
        )
    if isinstance(cause, causation.ResourceWatchingCause):
        new_kwargs.update(
            event=cause.raw,
            type=cause.type,
        )
    if isinstance(cause, causation.ResourceChangingCause):
        new_kwargs.update(
            event=cause.reason,  # deprecated; kept for backward-compatibility
            reason=cause.reason,
            diff=cause.diff,
            old=cause.old,
            new=cause.new,
        )

    return new_kwargs


async def invoke(
        fn: Invokable,
        *args: Any,
        cause: Optional[causation.BaseCause] = None,
        **kwargs: Any,
) -> Any:
    """
    Invoke a single function, but safely for the main asyncio process.

    Used mostly for handler functions, and potentially slow & blocking code.
    Other callbacks are called directly, and are expected to be synchronous
    (such as handler-selecting (lifecycles) and resource-filtering (``when=``)).

    A full set of the arguments is provided, expanding the cause to some easily
    usable aliases. The function is expected to accept ``**kwargs`` for the args
    that it does not use -- for forward compatibility with the new features.

    The synchronous methods are executed in the executor (threads or processes),
    thus making it non-blocking for the main event loop of the operator.
    See: https://pymotw.com/3/asyncio/executors.html
    """
    kwargs = build_kwargs(cause=cause, **kwargs)

    if is_async_fn(fn):
        result = await fn(*args, **kwargs)  # type: ignore
    else:

        # Not that we want to use functools, but for executors kwargs, it is officially recommended:
        # https://docs.python.org/3/library/asyncio-eventloop.html#asyncio.loop.run_in_executor
        real_fn = functools.partial(fn, *args, **kwargs)

        # Copy the asyncio context from current thread to the handlr's thread.
        # It can be copied 2+ times if there are sub-sub-handlers (rare case).
        context = contextvars.copy_context()
        real_fn = functools.partial(context.run, real_fn)

        # Prevent orphaned threads during daemon/handler cancellation. It is better to be stuck
        # in the task than to have orphan threads which deplete the executor's pool capacity.
        # Cancellation is postponed until the thread exits, but it happens anyway (for consistency).
        # Note: the docs say the result is a future, but typesheds say it is a coroutine => cast()!
        loop = asyncio.get_event_loop()
        executor = config.WorkersConfig.get_syn_executor()
        future = cast(asyncio_Future, loop.run_in_executor(executor, real_fn))
        cancellation: Optional[asyncio.CancelledError] = None
        while not future.done():
            try:
                await asyncio.shield(future)  # slightly expensive: creates tasks
            except asyncio.CancelledError as e:
                cancellation = e
        if cancellation is not None:
            raise cancellation
        result = future.result()

    return result


def is_async_fn(
        fn: Optional[Invokable],
) -> bool:
    if fn is None:
        return False
    elif isinstance(fn, functools.partial):
        return is_async_fn(fn.func)
    elif hasattr(fn, '__wrapped__'):  # @functools.wraps()
        return is_async_fn(fn.__wrapped__)  # type: ignore
    else:
        return asyncio.iscoroutinefunction(fn)
