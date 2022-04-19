#  coding=utf-8
#  Copyright 2021-present, the Recognai S.L. team.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

"""
This module configures the api routes under /api prefix, and
set the required security dependencies if api security is enabled
"""

from fastapi import APIRouter, FastAPI

from .api import v1
from .commons.errors.base_errors import __ALL__
from .datasets import api as datasets
from .info import api as info
from .tasks import api as tasks
from .users import api as users

api_router = APIRouter(
    responses={error.HTTP_STATUS: error.api_documentation() for error in __ALL__}
)

dependencies = []
for router in [users.router, datasets.router, info.router, tasks.router]:
    api_router.include_router(router, dependencies=dependencies)


api_v1 = FastAPI(
    description="Rubrix API v1 **EXPERIMENTAL**",
    # Disable default openapi configuration
    redoc_url=None,
    version="v1",
    responses={error.HTTP_STATUS: error.api_documentation() for error in __ALL__},
)

for router in [v1.api_router]:
    api_v1.include_router(router, dependencies=dependencies)

api_app = FastAPI()
api_app.mount("/v1", api_v1)
