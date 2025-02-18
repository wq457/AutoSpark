import datetime
import os
from pathlib import Path

import boto3
from botocore.exceptions import NoCredentialsError
from fastapi import APIRouter
from fastapi import File, Form, UploadFile
from fastapi import HTTPException, Depends
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBearer
from fastapi_jwt_auth import AuthJWT
from fastapi_sqlalchemy import db

from autospark.config.config import get_config
from autospark.helper.auth import check_auth
from autospark.helper.resource_helper import ResourceHelper
from autospark.lib.logger import logger
from autospark.models.agent import Agent
from autospark.models.resource import Resource
from autospark.worker import summarize_resource
from autospark.types.storage_types import StorageType

router = APIRouter()

s3 = boto3.client(
    's3',
    aws_access_key_id=get_config("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=get_config("AWS_SECRET_ACCESS_KEY"),
)


@router.post("/add/{agent_id}",dependencies=[Depends(HTTPBearer())], status_code=201)
async def upload(agent_id: int, file: UploadFile = File(...), name=Form(...), size=Form(...), type=Form(...),
                 Authorize: AuthJWT = Depends(check_auth)):
    """
    Upload a file as a resource for an agent.

    Args:
        agent_id (int): ID of the agent.
        file (UploadFile): Uploaded file.
        name (str): Name of the resource.
        size (str): Size of the resource.
        type (str): Type of the resource.

    Returns:
        Resource: Uploaded resource.

    Raises:
        HTTPException (status_code=400): If the agent with the specified ID does not exist.
        HTTPException (status_code=400): If the file type is not supported.
        HTTPException (status_code=500): If AWS credentials are not found or if there is an issue uploading to S3.

    """

    agent = db.session.query(Agent).filter(Agent.id == agent_id).first()
    if agent is None:
        raise HTTPException(status_code=400, detail="Agent does not exists")

    # accepted_file_types is a tuple because endswith() expects a tuple
    accepted_file_types = (".pdf", ".docx", ".pptx", ".csv", ".txt", ".epub")
    if not name.endswith(accepted_file_types):
        raise HTTPException(status_code=400, detail="File type not supported!")

    storage_type = StorageType.get_storage_type(get_config("STORAGE_TYPE", StorageType.FILE.value))
    save_directory = ResourceHelper.get_root_input_dir()
    if "{agent_id}" in save_directory:
        save_directory = ResourceHelper.get_formatted_agent_level_path(agent=Agent
                                                                       .get_agent_from_id(session=db.session,
                                                                                    agent_id=agent_id),
                                                                       path=save_directory)
    file_path = os.path.join(save_directory, file.filename)
    if storage_type == StorageType.FILE:
        os.makedirs(save_directory, exist_ok=True)
        with open(file_path, "wb") as f:
            contents = await file.read()
            f.write(contents)
            file.file.close()
    elif storage_type == StorageType.S3:
        bucket_name = get_config("BUCKET_NAME")
        file_path = 'resources' + file_path
        try:
            s3.upload_fileobj(file.file, bucket_name, file_path)
            logger.info("File uploaded successfully!")
        except NoCredentialsError:
            raise HTTPException(status_code=500, detail="AWS credentials not found. Check your configuration.")

    resource = Resource(name=name, path=file_path, storage_type=storage_type.value, size=size, type=type, channel="INPUT",
                        agent_id=agent.id)

    db.session.add(resource)
    db.session.commit()
    db.session.flush()

    summarize_resource.delay(agent_id, resource.id)
    logger.info(resource)

    return resource


@router.get("/get/all/{agent_id}", dependencies=[Depends(HTTPBearer())],status_code=200)
def get_all_resources(agent_id: int,
                      Authorize: AuthJWT = Depends(check_auth)):
    """
    Get all resources for an agent.

    Args:
        agent_id (int): ID of the agent.

    Returns:
        List[Resource]: List of resources belonging to the agent.

    """

    resources = db.session.query(Resource).filter(Resource.agent_id == agent_id).all()
    return resources


@router.get("/get/{resource_id}",dependencies=[Depends(HTTPBearer())], status_code=200)
def download_file_by_id(resource_id: int,
                        Authorize: AuthJWT = Depends(check_auth)):
    """
    Download a particular resource by resource_id.

    Args:
        resource_id (int): ID of the resource.
        Authorize (AuthJWT, optional): Authorization dependency.

    Returns:
        StreamingResponse: Streaming response for downloading the resource.

    Raises:
        HTTPException (status_code=400): If the resource with the specified ID is not found.
        HTTPException (status_code=404): If the file is not found.

    """

    resource = db.session.query(Resource).filter(Resource.id == resource_id).first()
    download_file_path = resource.path
    file_name = resource.name

    if not resource:
        raise HTTPException(status_code=400, detail="Resource Not found!")

    if resource.storage_type == StorageType.S3.value:
        bucket_name = get_config("BUCKET_NAME")
        file_key = resource.path
        response = s3.get_object(Bucket=bucket_name, Key=file_key)
        content = response["Body"]
    else:
        abs_file_path = Path(download_file_path).resolve()
        if not abs_file_path.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        content = open(str(abs_file_path), "rb")

    return StreamingResponse(
        content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename={file_name}"
        }
    )
