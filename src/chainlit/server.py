import mimetypes

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")

import os
import json
import webbrowser

from contextlib import asynccontextmanager
from watchfiles import awatch

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    FileResponse,
    PlainTextResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi_socketio import SocketManager
from starlette.middleware.cors import CORSMiddleware
import asyncio

from chainlit.config import config, load_module, DEFAULT_HOST
from chainlit.session import Session, sessions
from chainlit.user_session import user_sessions
from chainlit.client import CloudClient
from chainlit.emitter import ChainlitEmitter
from chainlit.markdown import get_markdown_str
from chainlit.action import Action
from chainlit.message import Message, ErrorMessage
from chainlit.telemetry import trace_event
from chainlit.logger import logger
from chainlit.types import CompletionRequest


@asynccontextmanager
async def lifespan(app: FastAPI):
    host = config.run_settings.host
    port = config.run_settings.port

    if not config.run_settings.headless:
        if host == DEFAULT_HOST:
            url = f"http://localhost:{port}"
        else:
            url = f"http://{host}:{port}"

        logger.info(f"Your app is available at {url}")
        webbrowser.open(url)

    watch_task = None
    stop_event = asyncio.Event()

    if config.run_settings.watch:

        async def watch_files_for_changes():
            async for changes in awatch(config.root, stop_event=stop_event):
                for change_type, file_path in changes:
                    file_name = os.path.basename(file_path)
                    file_ext = os.path.splitext(file_name)[1]

                    if file_ext.lower() == ".py" or file_name.lower() == "chainlit.md":
                        logger.info(f"File {change_type.name}: {file_name}")

                        # Reload the module if the module name is specified in the config
                        if config.module_name:
                            load_module(config.module_name)

                        await socket.emit("reload", {})

                        break

        watch_task = asyncio.create_task(watch_files_for_changes())

    try:
        yield
    except KeyboardInterrupt:
        logger.error("KeyboardInterrupt received, stopping the watch task...")
    finally:
        if watch_task:
            stop_event.set()
            await watch_task


root_dir = os.path.dirname(os.path.abspath(__file__))
build_dir = os.path.join(root_dir, "frontend/dist")

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=build_dir), name="static")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Define max HTTP data size to 100 MB
max_http_data_size = 100 * 1024 * 1024

socket = SocketManager(
    app,
    cors_allowed_origins=[],
    async_mode="asgi",
    max_http_buffer_size=max_http_data_size,
)

"""
-------------------------------------------------------------------------------
                              HTTP HANDLERS
-------------------------------------------------------------------------------
"""


def get_html_template():
    PLACEHOLDER = "<!-- TAG INJECTION PLACEHOLDER -->"

    default_url = "https://github.com/Chainlit/chainlit"
    url = config.github or default_url

    tags = f"""<title>{config.chatbot_name}</title>
    <meta name="description" content="{config.description}">
    <meta property="og:type" content="website">
    <meta property="og:title" content="{config.chatbot_name}">
    <meta property="og:description" content="{config.description}">
    <meta property="og:image" content="https://chainlit-cloud.s3.eu-west-3.amazonaws.com/logo/chainlit_banner.png">
    <meta property="og:url" content="{url}">"""

    index_html_file_path = os.path.join(build_dir, "index.html")

    with open(index_html_file_path, "r", encoding="utf-8") as f:
        content = f.read()
        content = content.replace(PLACEHOLDER, tags)
        return content


def get_html_template_mod(path: str):
    PLACEHOLDER = "<!-- TAG INJECTION PLACEHOLDER -->"

    default_url = "https://github.com/Chainlit/chainlit"
    url = config.github or default_url

    tags = f"""<title>{config.chatbot_name}</title>
    <meta name="description" content="{config.description}">
    <meta property="og:type" content="website">
    <meta property="og:title" content="{config.chatbot_name}">
    <meta property="og:description" content="{config.description}">
    <meta property="og:image" content="https://chainlit-cloud.s3.eu-west-3.amazonaws.com/logo/chainlit_banner.png">
    <meta property="og:url" content="{url}">
    <meta property="og:modelPath" content="{path}">"""

    index_html_file_path = os.path.join(build_dir, "index.html")

    with open(index_html_file_path, "r", encoding="utf-8") as f:
        content = f.read()
        content = content.replace(PLACEHOLDER, tags)
        return content


html_template = get_html_template()


@app.post("/completion")
async def completion(completion: CompletionRequest):
    """Handle a completion request from the prompt playground."""

    import openai

    trace_event("completion")

    api_key = completion.userEnv.get("OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY"))

    model_name = completion.settings.model_name
    stop = completion.settings.stop
    # OpenAI doesn't support an empty stop array, clear it
    if isinstance(stop, list) and len(stop) == 0:
        stop = None

    if model_name in ["gpt-3.5-turbo", "gpt-4"]:
        response = await openai.ChatCompletion.acreate(
            api_key=api_key,
            model=model_name,
            messages=[{"role": "user", "content": completion.prompt}],
            stop=stop,
            **completion.settings.to_settings_dict(),
        )
        return PlainTextResponse(content=response["choices"][0]["message"]["content"])
    else:
        response = await openai.Completion.acreate(
            api_key=api_key,
            model=model_name,
            prompt=completion.prompt,
            stop=stop,
            **completion.settings.to_settings_dict(),
        )
        return PlainTextResponse(content=response["choices"][0]["text"])


@app.get("/project/settings")
async def project_settings():
    """Return project settings. This is called by the UI before the establishing the websocket connection."""
    return JSONResponse(
        content={
            "public": config.public,
            "projectId": config.project_id,
            "chainlitServer": config.chainlit_server,
            "userEnv": config.user_env,
            "hideCot": config.hide_cot,
            "chainlitMd": get_markdown_str(config.root),
            "prod": bool(config.chainlit_prod_url),
            "appTitle": config.chatbot_name,
            "github": config.github,
        }
    )


@app.get("/model_exists")
async def mode_exists(model: str):
    """Return project settings. This is called by the UI before the establishing the websocket connection."""
    # Logic to determine if the model is already loaded in the server / database
    exists_bool = False
    path_saved_models = "SavedModels/"
    path_model = path_saved_models + model
    print("path to saved models: " + path_saved_models)

    if os.path.exists(path_model):
        print("File exists!")
        exists_bool = True
    else:
        print("File does not exist.")

    return JSONResponse(content={"model": model, "exists": exists_bool})


# @app.post("/new_model/")
# async def new_model(request: Request):
#    data = await request.json()


@app.post("/upload_json/")
async def upload_json(file: UploadFile):
    path_saved_models = "SavedModels/"
    # Check if the uploaded file is a JSON file
    if file.content_type != "application/json":
        raise HTTPException(status_code=400, detail="Uploaded file is not a JSON file.")

    # Read the contents of the uploaded file
    contents = await file.read()

    try:
        # Attempt to parse the JSON contents
        json_data = json.loads(contents)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Uploaded file is not valid JSON.")

    # Save the JSON file to disk
    file_path = os.path.join(path_saved_models, file.filename)
    with open(file_path, "w") as f:
        f.write(json.dumps(json_data))

    return {"message": "JSON file saved successfully."}


@app.get("/{path:path}")
async def serve(path: str):
    """Serve the UI."""
    path_to_file = os.path.join(build_dir, path)
    if path != "" and os.path.exists(path_to_file):
        return FileResponse(path_to_file)
    else:
        headers = {"path": path}
        return HTMLResponse(
            content=get_html_template_mod(path), status_code=200, headers=headers
        )
        # return HTMLResponse(content=html_template, status_code=200, headers=headers)


"""
-------------------------------------------------------------------------------
                              WEBSOCKET HANDLERS
-------------------------------------------------------------------------------
"""


def need_session(id: str):
    """Return the session with the given id."""

    session = sessions.get(id)
    if not session:
        raise ValueError("Session not found")
    return session


@socket.on("connect")
async def connect(sid, environ):
    user_env = environ.get("HTTP_USER_ENV")
    authorization = environ.get("HTTP_AUTHORIZATION")
    cloud_client = None

    # Check decorated functions
    if not config.lc_factory and not config.on_message and not config.on_chat_start:
        logger.error(
            "Module should at least expose one of @langchain_factory, @on_message or @on_chat_start function"
        )
        return False

    # Check authorization
    if not config.public and not authorization:
        # Refuse connection if the app is private and no access token is provided
        trace_event("no_access_token")
        logger.error("No access token provided")
        return False
    elif authorization and config.project_id:
        # Create the cloud client
        cloud_client = CloudClient(
            project_id=config.project_id,
            session_id=sid,
            access_token=authorization,
        )
        is_project_member = await cloud_client.is_project_member()
        if not is_project_member:
            logger.error("You are not a member of this project")
            return False

    # Check user env
    if config.user_env:
        # Check if requested user environment variables are provided
        if user_env:
            user_env = json.loads(user_env)
            for key in config.user_env:
                if key not in user_env:
                    trace_event("missing_user_env")
                    logger.error("Missing user environment variable: " + key)
                    return False
        else:
            logger.error("Missing user environment variables")
            return False

    # Create the session

    # Function to send a message to this particular session
    def emit_fn(event, data):
        if sid in sessions:
            if sessions[sid]["should_stop"]:
                sessions[sid]["should_stop"] = False
                raise InterruptedError("Task stopped by user")
        return socket.emit(event, data, to=sid)

    # Function to ask the user a question
    def ask_user_fn(data, timeout):
        if sessions[sid]["should_stop"]:
            sessions[sid]["should_stop"] = False
            raise InterruptedError("Task stopped by user")
        return socket.call("ask", data, timeout=timeout, to=sid)

    session = {
        "id": sid,
        "emit": emit_fn,
        "ask_user": ask_user_fn,
        "client": cloud_client,
        "user_env": user_env,
        "running_sync": False,
        "should_stop": False,
        "params": dict(),
    }  # type: Session

    sessions[sid] = session

    trace_event("connection_successful")
    return True


# @socket.on("connection_successful")
# async def connection_successful(sid):
#    session = need_session(sid)
#    __chainlit_emitter__ = ChainlitEmitter(session)
#    if config.lc_factory:
#        """Instantiate the langchain agent and store it in the session."""
#        agent = await config.lc_factory(__chainlit_emitter__=__chainlit_emitter__)
#        session["agent"] = agent

#    if config.on_chat_start:
#        """Call the on_chat_start function provided by the developer."""
#        await config.on_chat_start(__chainlit_emitter__=__chainlit_emitter__)


@socket.on("connection_successful")
async def connection_successful(sid, param):
    session = need_session(sid)
    __chainlit_emitter__ = ChainlitEmitter(session)
    # print('from chainlit: '+ param)
    session["params"]["param1"] = param
    if config.lc_factory:
        """Instantiate the langchain agent and store it in the session."""
        agent = await config.lc_factory(__chainlit_emitter__=__chainlit_emitter__)
        session["agent"] = agent

    if config.on_chat_start:
        """Call the on_chat_start function provided by the developer."""
        await config.on_chat_start(__chainlit_emitter__=__chainlit_emitter__)


@socket.on("disconnect")
async def disconnect(sid):
    if sid in sessions:
        # Clean up the session
        sessions.pop(sid)

    if sid in user_sessions:
        # Clean up the user session
        user_sessions.pop(sid)


@socket.on("stop")
async def stop(sid):
    if sid in sessions:
        trace_event("stop_task")
        session = sessions[sid]

        __chainlit_emitter__ = ChainlitEmitter(session)

        await Message(author="System", content="Task stopped by the user.").send()

        session["should_stop"] = True

        if config.on_stop:
            await config.on_stop()


async def process_message(session: Session, author: str, input_str: str):
    """Process a message from the user."""

    try:
        __chainlit_emitter__ = ChainlitEmitter(session)
        await __chainlit_emitter__.task_start()

        if session["client"]:
            # If cloud is enabled, persist the message
            await session["client"].create_message(
                {
                    "author": author,
                    "content": input_str,
                    "authorIsUser": True,
                }
            )

        langchain_agent = session.get("agent")
        if langchain_agent:
            from chainlit.lc.agent import run_langchain_agent

            # If a langchain agent is available, run it
            if config.lc_run:
                # If the developer provided a custom run function, use it
                await config.lc_run(
                    langchain_agent,
                    input_str,
                    __chainlit_emitter__=__chainlit_emitter__,
                )
                return
            else:
                # Otherwise, use the default run function
                raw_res, output_key = await run_langchain_agent(
                    langchain_agent, input_str, use_async=config.lc_agent_is_async
                )

                if config.lc_postprocess:
                    # If the developer provided a custom postprocess function, use it
                    await config.lc_postprocess(
                        raw_res, __chainlit_emitter__=__chainlit_emitter__
                    )
                    return
                elif output_key is not None:
                    # Use the output key if provided
                    res = raw_res[output_key]
                else:
                    # Otherwise, use the raw response
                    res = raw_res
            # Finally, send the response to the user
            await Message(author=config.chatbot_name, content=res).send()

        elif config.on_message:
            # If no langchain agent is available, call the on_message function provided by the developer
            await config.on_message(
                input_str, __chainlit_emitter__=__chainlit_emitter__
            )
    except InterruptedError:
        pass
    except Exception as e:
        logger.exception(e)
        await ErrorMessage(author="Error", content=str(e)).send()
    finally:
        await __chainlit_emitter__.task_end()


@socket.on("ui_message")
async def message(sid, data):
    """Handle a message sent by the User."""
    session = need_session(sid)
    session["should_stop"] = False

    input_str = data["content"].strip()
    author = data["author"]

    await process_message(session, author, input_str)


async def process_action(session: Session, action: Action):
    __chainlit_emitter__ = ChainlitEmitter(session)
    callback = config.action_callbacks.get(action.name)
    if callback:
        await callback(action, __chainlit_emitter__=__chainlit_emitter__)
    else:
        logger.warning("No callback found for action %s", action.name)


@socket.on("action_call")
async def call_action(sid, action):
    """Handle an action call from the UI."""
    session = need_session(sid)

    __chainlit_emitter__ = ChainlitEmitter(session)
    action = Action(**action)

    await process_action(session, action)
