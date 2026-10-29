from __future__ import annotations

import base64
import json
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
from viewer.app_paths import user_data_dir


class Motion1080ApiError(RuntimeError):
    """
    Raised when the 1080 API cannot be reached or returns an invalid response.
    """


@dataclass(frozen=True)
class ApiConfig:
    """
    Configuration for the 1080 Motion REST API.
    """

    api_key: str
    api_host: str = "https://publicapi.1080motion.com"


class Motion1080Api:
    """
    REST client + normalizers for the 1080 Motion public API.

    This class intentionally contains no Qt/UI code.

    Important documented endpoints used by this client:
      - GET /Client
      - GET /Client/{clientId}
      - GET /Session/Search?client=...&maxAgeDays=...
      - GET /Session/{sessionId}
      - GET /TrainingData/Set/{setId}?includeSamples=true&filterMode=None
    """

    DEFAULT_API_HOST = "https://publicapi.1080motion.com"
    ENV_API_KEY = "1080_API_KEY"
    ENV_API_HOST = "1080_API_HOST"

    def __init__(
        self,
        api_key: str,
        api_host: str = DEFAULT_API_HOST,
        timeout_seconds: float = 30.0,
    ):
        api_key = str(api_key or "").strip()
        api_host = str(api_host or self.DEFAULT_API_HOST).strip().rstrip("/")

        if not api_key:
            raise ValueError(
                "Missing 1080 API key. Set 1080_API_KEY or save it in config.json."
            )

        if not api_host:
            raise ValueError("Missing 1080 API host.")

        self.api_key = api_key
        self.api_host = api_host
        self.timeout_seconds = float(timeout_seconds)

        self.session = requests.Session()
        self.session.headers.update(
            {
                "X-1080-API-Key": self.api_key,
                "Accept": "application/json",
            }
        )

    # ==================================================
    # CONFIG PATHS / STORAGE
    # ==================================================
    @classmethod
    def default_config_path(cls) -> Path:
        return user_data_dir() / "config.json"

    @classmethod
    def load_local_config(cls, config_path: Path | None = None) -> dict[str, Any]:
        if config_path is None:
            config_path = cls.default_config_path()

        config_path = Path(config_path)

        if not config_path.exists():
            return {}

        try:
            with config_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            raise Motion1080ApiError(
                f"Invalid JSON in 1080 API config file: {config_path}"
            ) from exc

        if not isinstance(data, dict):
            raise Motion1080ApiError(
                f"1080 API config file must contain a JSON object: {config_path}"
            )

        return data

    @classmethod
    def save_api_key_to_config(
        cls,
        api_key: str,
        api_host: str = DEFAULT_API_HOST,
        config_path: Path | None = None,
    ) -> Path:
        api_key = str(api_key or "").strip()
        api_host = str(api_host or cls.DEFAULT_API_HOST).strip().rstrip("/")

        if not api_key:
            raise ValueError("Cannot save an empty 1080 API key.")

        if config_path is None:
            config_path = cls.default_config_path()

        config_path = Path(config_path)
        config_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "api_key": api_key,
            "api_host": api_host,
        }

        tmp_path = config_path.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")

        tmp_path.replace(config_path)
        return config_path

    @classmethod
    def write_local_config(
        cls,
        api_key: str,
        api_host: str = DEFAULT_API_HOST,
        config_path: Path | None = None,
    ) -> Path:
        return cls.save_api_key_to_config(
            api_key=api_key,
            api_host=api_host,
            config_path=config_path,
        )

    @classmethod
    def has_saved_api_key(cls, config_path: Path | None = None) -> bool:
        env_key = os.environ.get(cls.ENV_API_KEY)
        if env_key and env_key.strip():
            return True

        config = cls.load_local_config(config_path)
        saved_key = config.get("api_key")
        return bool(str(saved_key or "").strip())

    @classmethod
    def resolve_api_key_and_host(
        cls,
        config_path: Path | None = None,
    ) -> ApiConfig:
        config = cls.load_local_config(config_path)

        api_key = os.environ.get(cls.ENV_API_KEY) or config.get("api_key") or ""
        api_host = (
            os.environ.get(cls.ENV_API_HOST)
            or config.get("api_host")
            or cls.DEFAULT_API_HOST
        )

        api_key = str(api_key or "").strip()
        api_host = str(api_host or cls.DEFAULT_API_HOST).strip().rstrip("/")

        if not api_key:
            raise Motion1080ApiError("No 1080 API key has been configured yet.")

        return ApiConfig(api_key=api_key, api_host=api_host)

    @classmethod
    def from_environment_or_config(
        cls,
        config_path: Path | None = None,
        timeout_seconds: float = 30.0,
    ) -> "Motion1080Api":
        config = cls.resolve_api_key_and_host(config_path)
        return cls(
            api_key=config.api_key,
            api_host=config.api_host,
            timeout_seconds=timeout_seconds,
        )

    @classmethod
    def delete_local_config(cls, config_path: Path | None = None) -> None:
        if config_path is None:
            config_path = cls.default_config_path()

        config_path = Path(config_path)
        if config_path.exists():
            config_path.unlink()

    # ==================================================
    # LOW-LEVEL HTTP
    # ==================================================
    def build_url(self, relative_path: str) -> str:
        relative_path = str(relative_path or "").strip()
        if not relative_path:
            raise ValueError("API relative path cannot be empty.")
        return f"{self.api_host}/{relative_path.lstrip('/')}"

    def get(self, relative_path: str) -> Any:
        url = self.build_url(relative_path)

        try:
            response = self.session.get(url, timeout=self.timeout_seconds)
        except requests.RequestException as exc:
            raise Motion1080ApiError(
                f"Could not connect to 1080 API: {url}\n{exc}"
            ) from exc

        return self._handle_response(response, url)

    def get_bytes(self, relative_path: str) -> bytes:
        url = self.build_url(relative_path)

        try:
            response = self.session.get(url, timeout=self.timeout_seconds)
        except requests.RequestException as exc:
            raise Motion1080ApiError(
                f"Could not connect to 1080 API: {url}\n{exc}"
            ) from exc

        if not response.ok:
            raise self._error_from_response(response, url)

        return response.content

    def _handle_response(self, response: requests.Response, url: str) -> Any:
        if not response.ok:
            raise self._error_from_response(response, url)

        if not response.content:
            return None

        try:
            return response.json()
        except ValueError as exc:
            preview = response.text[:500]
            raise Motion1080ApiError(
                f"1080 API returned non-JSON response from {url}:\n{preview}"
            ) from exc

    def _error_from_response(
        self,
        response: requests.Response,
        url: str,
    ) -> Motion1080ApiError:
        status = response.status_code
        reason = response.reason or ""

        try:
            body = response.json()
            body_text = json.dumps(body, indent=2, ensure_ascii=False)
        except Exception:
            body_text = response.text[:1000]

        if status == 401:
            message = (
                "1080 API authentication failed. Check that the API key is valid "
                "and that it is passed through X-1080-API-Key."
            )
        elif status == 403:
            message = (
                "1080 API access forbidden. The API key may not have permission "
                "to access this organization or resource."
            )
        elif status == 404:
            message = "1080 API resource not found."
        else:
            message = "1080 API request failed."

        return Motion1080ApiError(
            f"{message}\n\n"
            f"URL: {url}\n"
            f"Status: {status} {reason}\n"
            f"Response:\n{body_text}"
        )

    # ==================================================
    # KNOWN API ENDPOINTS
    # ==================================================
    def get_clients(self) -> list[dict[str, Any]]:
        data = self.get("Client")
        return self._ensure_list(data, "Client")

    def get_client(self, client_id: str) -> dict[str, Any]:
        client_id = str(client_id or "").strip()
        if not client_id:
            raise ValueError("client_id cannot be empty.")

        data = self.get(f"Client/{client_id}")
        return self._ensure_dict(data, "Client/{clientId}")

    def get_sessions(
        self,
        max_age_days: int = -1,
        client_id: str | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Search for sessions.

        Correct public endpoint:
            GET /Session/Search?client=<client_id>&maxAgeDays=-1
        """
        params: dict[str, Any] = {
            "maxAgeDays": int(max_age_days),
        }

        if client_id:
            params["client"] = str(client_id)

        if extra_params:
            params.update(extra_params)

        query = urlencode(params)
        data = self.get(f"Session/Search?{query}")
        return self._ensure_list(data, "Session/Search")

    def get_session(self, session_id: str) -> dict[str, Any]:
        session_id = str(session_id or "").strip()
        if not session_id:
            raise ValueError("session_id cannot be empty.")

        data = self.get(f"Session/{session_id}")
        return self._ensure_dict(data, "Session/{sessionId}")

    def get_session_details(self, session_id: str) -> dict[str, Any]:
        return self.get_session(session_id)

    def get_training_data_set(
        self,
        set_id: str,
        *,
        include_samples: bool = True,
        filter_mode: str = "None",
    ) -> dict[str, Any]:
        """
        Get training data for one specific set.

        Correct public endpoint:
            GET /TrainingData/Set/{setId}?includeSamples=true&filterMode=None
        """
        set_id = str(set_id or "").strip()
        if not set_id:
            raise ValueError("set_id cannot be empty.")

        params = {
            "includeSamples": "true" if include_samples else "false",
            "filterMode": str(filter_mode or "None"),
        }

        query = urlencode(params)
        data = self.get(f"TrainingData/Set/{set_id}?{query}")
        return self._ensure_dict(data, "TrainingData/Set/{setId}")

    def get_training_data_session(
        self,
        session_id: str,
        *,
        include_samples: bool = True,
        filter_mode: str = "None",
    ) -> list[dict[str, Any]]:
        session_id = str(session_id or "").strip()
        if not session_id:
            raise ValueError("session_id cannot be empty.")

        params = {
            "includeSamples": "true" if include_samples else "false",
            "filterMode": str(filter_mode or "None"),
        }
        query = urlencode(params)
        data = self.get(f"TrainingData/Session/{session_id}?{query}")
        return self._ensure_list(data, "TrainingData/Session/{sessionId}")

    def get_training_data_exercise(
        self,
        exercise_id: str,
        *,
        include_samples: bool = True,
        filter_mode: str = "None",
    ) -> list[dict[str, Any]]:
        exercise_id = str(exercise_id or "").strip()
        if not exercise_id:
            raise ValueError("exercise_id cannot be empty.")

        params = {
            "includeSamples": "true" if include_samples else "false",
            "filterMode": str(filter_mode or "None"),
        }
        query = urlencode(params)
        data = self.get(f"TrainingData/Exercise/{exercise_id}?{query}")
        return self._ensure_list(data, "TrainingData/Exercise/{exerciseId}")

    def search_training_data(
        self,
        *,
        client_id: str | None = None,
        max_age_days: int = -1,
        include_samples: bool = False,
        filter_mode: str = "None",
        max_results: int = 20,
        from_date: str | None = None,
        to_date: str | None = None,
        exercise_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "maxAgeDays": int(max_age_days),
            "includeSamples": "true" if include_samples else "false",
            "filterMode": str(filter_mode or "None"),
            "maxResults": int(max_results),
        }

        if client_id:
            params["client"] = str(client_id)
        if from_date:
            params["fromDate"] = str(from_date)
        if to_date:
            params["toDate"] = str(to_date)
        if exercise_types:
            params["exerciseTypes"] = ",".join(str(x) for x in exercise_types)

        query = urlencode(params)
        data = self.get(f"TrainingData/Search?{query}")
        return self._ensure_list(data, "TrainingData/Search")

    # Backwards-compatible helper for older worker code.
    def get_training_data_for_set(
        self,
        *,
        set_id: str,
        session_id: str | None = None,
        client_id: str | None = None,
        max_age_days: int = -1,
        include_samples: bool = True,
        max_results: int = 50,
    ) -> tuple[str, dict[str, Any]]:
        del session_id, client_id, max_age_days, max_results
        return (
            f"TrainingData/Set/{set_id}",
            self.get_training_data_set(set_id, include_samples=include_samples),
        )

    # ==================================================
    # FLEXIBLE / DISCOVERY HELPERS
    # ==================================================
    def get_relative(self, relative_path: str) -> Any:
        return self.get(relative_path)

    def try_get_first_success(self, relative_paths: list[str]) -> tuple[str, Any]:
        errors: list[str] = []

        for path in relative_paths:
            try:
                return path, self.get(path)
            except Exception as exc:
                errors.append(f"{path}: {exc}")

        raise Motion1080ApiError(
            "None of the candidate 1080 API endpoints succeeded:\n\n"
            + "\n\n".join(errors)
        )

    # ==================================================
    # NORMALIZATION: CLIENTS / SESSIONS / SETS / RUNS
    # ==================================================
    def normalize_clients(self, clients: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []

        for client in clients:
            if not isinstance(client, dict):
                continue

            client_id = self._first_value(client, ["id", "clientId", "clientID", "guid"])
            display_name = self._first_value(
                client,
                ["displayName", "name", "fullName", "clientName"],
                fallback=str(client_id or "Unnamed client"),
            )

            normalized.append(
                {
                    "id": str(client_id or ""),
                    "display_name": str(display_name or "Unnamed client"),
                    "group": client.get("group"),
                    "external_id": client.get("externalId"),
                    "_raw": client,
                }
            )

        normalized.sort(key=lambda item: item["display_name"].lower())
        return normalized

    def normalize_sessions(
        self,
        sessions: list[dict[str, Any]],
        client_id: str | None = None,
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []

        for session in sessions:
            if not isinstance(session, dict):
                continue

            raw_client_id = self._first_value(
                session,
                ["clientId", "clientID", "client_id", "client"],
            )

            if client_id and raw_client_id and str(raw_client_id) != str(client_id):
                continue

            session_id = self._first_value(
                session,
                ["id", "sessionId", "sessionID", "guid"],
            )

            timestamp = self._first_value(
                session,
                ["timestamp", "date", "created", "startTime", "started", "time"],
            )

            name = self._first_value(
                session,
                ["name", "displayName", "title", "sessionName"],
                fallback=None,
            )

            if not name:
                name = str(timestamp)[:19] if timestamp else "Unnamed session"

            normalized.append(
                {
                    "id": str(session_id or ""),
                    "client_id": str(raw_client_id or client_id or ""),
                    "display_name": str(name or "Unnamed session"),
                    "date": timestamp,
                    "set_count": self._infer_set_count(session),
                    "_raw": session,
                }
            )

        normalized.sort(key=lambda item: str(item.get("date") or ""), reverse=True)
        return normalized

    def normalize_sets_from_session(self, session: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Extract real sets from a session detail object.

        The 1080 API structure is:
            session -> exercises[] -> sets[]

        The sidebar flattens all exercise sets into one list.
        """
        if not isinstance(session, dict):
            return []

        exercises = session.get("exercises") or session.get("Exercises") or []
        if not isinstance(exercises, list):
            return []

        normalized: list[dict[str, Any]] = []
        global_set_index = 0

        for exercise_index, exercise in enumerate(exercises, start=1):
            if not isinstance(exercise, dict):
                continue

            exercise_id = str(exercise.get("id") or "")
            exercise_type_id = str(exercise.get("exerciseTypeId") or "")
            exercise_name = (
                exercise.get("exerciseTypeName")
                or exercise.get("exerciseName")
                or exercise.get("name")
                or f"Exercise {exercise_index}"
            )

            sets = exercise.get("sets") or exercise.get("Sets") or []
            if not isinstance(sets, list):
                continue

            for set_index_in_exercise, set_obj in enumerate(sets, start=1):
                if not isinstance(set_obj, dict):
                    continue

                global_set_index += 1

                set_id = str(
                    set_obj.get("id")
                    or set_obj.get("setId")
                    or f"{exercise_id}_set_{set_index_in_exercise}"
                )

                external_load = set_obj.get("externalLoad")
                created = set_obj.get("created")
                view_settings = set_obj.get("viewSettings") or {}

                display_name = f"{exercise_name} - Set {set_index_in_exercise}"
                if external_load:
                    display_name += f" | Load {external_load}"

                normalized.append(
                    {
                        "id": set_id,
                        "display_name": display_name,
                        "index": global_set_index,
                        "set_index_in_exercise": set_index_in_exercise,
                        "exercise_id": exercise_id,
                        "exercise_type_id": exercise_type_id,
                        "exercise_name": str(exercise_name),
                        "created": created,
                        "external_load": external_load,
                        "view_settings": view_settings,
                        "_raw": set_obj,
                        "_exercise": exercise,
                    }
                )

        return normalized

    def normalize_runs_from_training_set(self, training_set: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Convert one TrainingData/Set response into UI runs.

        In the 1080 API, runs correspond to motionGroups. A motionGroup can
        contain one or more motions, and motions contain sampleData.
        """
        if not isinstance(training_set, dict):
            return []

        set_id = str(training_set.get("setId") or training_set.get("id") or "set")
        motion_groups = training_set.get("motionGroups") or []

        if not isinstance(motion_groups, list):
            return []

        normalized: list[dict[str, Any]] = []

        for run_index, group in enumerate(motion_groups, start=1):
            if not isinstance(group, dict):
                continue

            group_id = str(group.get("id") or f"{set_id}_run_{run_index}")
            side = group.get("side")
            comment = group.get("comment")
            motions = group.get("motions") or []

            if not isinstance(motions, list):
                motions = []

            first_motion = motions[0] if motions and isinstance(motions[0], dict) else {}

            resistance = first_motion.get("resistanceValues") or {}
            average_values = first_motion.get("averageValues") or {}
            peak_values = first_motion.get("peakValues") or {}

            if not isinstance(resistance, dict):
                resistance = {}
            if not isinstance(average_values, dict):
                average_values = {}
            if not isinstance(peak_values, dict):
                peak_values = {}

            concentric_load = resistance.get("concentricLoad")
            eccentric_load = resistance.get("eccentricLoad")
            mode = resistance.get("mode")

            load_text = None
            if concentric_load is not None and eccentric_load is not None:
                load_text = f"{concentric_load}/{eccentric_load}"
            elif concentric_load is not None:
                load_text = concentric_load
            elif eccentric_load is not None:
                load_text = eccentric_load

            if first_motion.get("isEccentric") is True:
                direction = "Eccentric"
            elif first_motion.get("isEccentric") is False:
                direction = "Concentric"
            else:
                direction = None

            if side and str(side).lower() != "unknown":
                direction = f"{direction or ''} {side}".strip()

            normalized.append(
                {
                    "id": group_id,
                    "display_name": f"Run {run_index}",
                    "index": run_index,
                    "distance": first_motion.get("totalDistance"),
                    "time": first_motion.get("totalTime"),
                    "top_speed": first_motion.get("topSpeed") or peak_values.get("speed"),
                    "load": load_text,
                    "direction": direction or mode,
                    "comment": comment,
                    "motion_count": len(motions),
                    "average_speed": average_values.get("speed"),
                    "average_force": average_values.get("force"),
                    "average_power": average_values.get("power"),
                    "peak_force": peak_values.get("force"),
                    "peak_power": peak_values.get("power"),
                    "_raw": group,
                    "_training_set": training_set,
                }
            )

        return normalized

    # Backwards-compatible fallback for older code paths.
    def normalize_runs_from_training_data(
        self,
        training_data: Any,
        *,
        fallback_set_id: str = "",
    ) -> list[dict[str, Any]]:
        del fallback_set_id
        if isinstance(training_data, list):
            if not training_data:
                return []
            training_data = training_data[0]
        return self.normalize_runs_from_training_set(training_data)

    def normalize_runs_from_set(self, set_obj: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Legacy local extraction only. This does not call TrainingData.
        Kept so older main_window imports do not break.
        """
        if not isinstance(set_obj, dict):
            return []

        raw_runs = self._find_run_list_in_nested_dict(set_obj)
        if isinstance(raw_runs, dict):
            raw_runs = list(raw_runs.values())
        if not isinstance(raw_runs, list):
            raw_runs = []

        normalized: list[dict[str, Any]] = []
        for idx, run_obj in enumerate(raw_runs, start=1):
            if not isinstance(run_obj, dict):
                continue
            run_id = self._first_value(
                run_obj,
                ["id", "runId", "motionGroupId", "motionId", "setDataId", "guid"],
                fallback=f"run_{idx}",
            )
            normalized.append(
                {
                    "id": str(run_id),
                    "display_name": f"Run {idx}",
                    "index": idx,
                    "distance": self._first_value(run_obj, ["distance", "totalDistance"]),
                    "time": self._first_value(run_obj, ["time", "totalTime", "duration"]),
                    "top_speed": self._first_value(run_obj, ["topSpeed", "maxSpeed"]),
                    "load": self._first_value(run_obj, ["load", "resistance"]),
                    "direction": self._first_value(run_obj, ["direction", "mode"]),
                    "_raw": run_obj,
                }
            )
        return normalized

    # ==================================================
    # SAMPLE DATA HELPERS
    # ==================================================
    @staticmethod
    def decode_sample_data_base64(sample_data: str) -> dict[str, list[float]]:
        """
        Decode 1080 sampleData.

        Each sample is 5 little-endian float32 values:
            time, position, speed, acceleration, force
        """
        if not sample_data:
            return {
                "time": [],
                "position": [],
                "speed": [],
                "acceleration": [],
                "force": [],
            }

        decoded = base64.b64decode(sample_data)
        bytes_per_sample = 20
        values_per_sample = 5

        if len(decoded) % bytes_per_sample != 0:
            raise Motion1080ApiError(
                "Invalid sample data length. Expected a multiple of 20 bytes."
            )

        n_samples = len(decoded) // bytes_per_sample
        if n_samples == 0:
            return {
                "time": [],
                "position": [],
                "speed": [],
                "acceleration": [],
                "force": [],
            }

        unpacked = struct.unpack(f"<{n_samples * values_per_sample}f", decoded)
        return {
            "time": list(unpacked[0::5]),
            "position": list(unpacked[1::5]),
            "speed": list(unpacked[2::5]),
            "acceleration": list(unpacked[3::5]),
            "force": list(unpacked[4::5]),
        }

    @staticmethod
    def extract_sample_data_field(data: Any) -> str | None:
        if isinstance(data, dict):
            for key in ["sampleData", "sample_data", "data", "encodedData", "binaryData"]:
                value = data.get(key)
                if isinstance(value, str) and value:
                    return value

            for value in data.values():
                found = Motion1080Api.extract_sample_data_field(value)
                if found:
                    return found

        elif isinstance(data, list):
            for item in data:
                found = Motion1080Api.extract_sample_data_field(item)
                if found:
                    return found

        return None

    @staticmethod
    def extract_all_sample_data_fields(data: Any) -> list[str]:
        found: list[str] = []

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key in ["sampleData", "sample_data", "encodedData", "binaryData"]:
                    sample = value.get(key)
                    if isinstance(sample, str) and sample:
                        found.append(sample)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(data)
        return found

    def decode_all_samples_concatenated(self, data: Any) -> dict[str, list[float]]:
        """
        Decode all sampleData fields found recursively and concatenate them.

        If multiple motions are present and each time vector restarts at 0,
        times are offset to keep the combined signal monotonic.
        """
        samples = self.extract_all_sample_data_fields(data)
        if not samples:
            return {
                "time": [],
                "position": [],
                "speed": [],
                "acceleration": [],
                "force": [],
            }

        combined = {
            "time": [],
            "position": [],
            "speed": [],
            "acceleration": [],
            "force": [],
        }

        time_offset = 0.0
        last_time = 0.0

        for i, sample in enumerate(samples):
            decoded = self.decode_sample_data_base64(sample)
            t = decoded["time"]
            if not t:
                continue

            if i == 0:
                adjusted_t = t
            else:
                # If the next segment restarts around zero, offset it.
                if t[0] <= last_time:
                    time_offset = last_time
                adjusted_t = [float(x) + float(time_offset) for x in t]

            combined["time"].extend(adjusted_t)
            combined["position"].extend(decoded["position"])
            combined["speed"].extend(decoded["speed"])
            combined["acceleration"].extend(decoded["acceleration"])
            combined["force"].extend(decoded["force"])
            last_time = combined["time"][-1]

        return combined

    # Legacy run-data candidates kept only as fallback.
    def fetch_run_data_candidates(self, run_id: str) -> tuple[str, Any]:
        run_id = str(run_id or "").strip()
        if not run_id:
            raise ValueError("run_id cannot be empty.")

        candidates = [
            f"TrainingData/Set/{run_id}?includeSamples=true&filterMode=None",
            f"SetData/{run_id}",
            f"Motion/{run_id}/Data",
            f"MotionData/{run_id}",
            f"RunData/{run_id}",
        ]
        return self.try_get_first_success(candidates)

    # ==================================================
    # VALIDATION / UTILS
    # ==================================================
    @staticmethod
    def _ensure_list(data: Any, endpoint_name: str) -> list[dict[str, Any]]:
        if data is None:
            return []
        if not isinstance(data, list):
            raise Motion1080ApiError(
                f"Expected {endpoint_name} response to be a list, got {type(data).__name__}."
            )
        return [item for item in data if isinstance(item, dict)]

    @staticmethod
    def _ensure_dict(data: Any, endpoint_name: str) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise Motion1080ApiError(
                f"Expected {endpoint_name} response to be an object, got {type(data).__name__}."
            )
        return data

    @staticmethod
    def _first_value(
        data: dict[str, Any],
        keys: list[str],
        fallback: Any = None,
    ) -> Any:
        for key in keys:
            if key in data and data[key] is not None:
                return data[key]
        return fallback

    @staticmethod
    def _is_int_like(value: Any) -> bool:
        try:
            int(value)
            return True
        except Exception:
            return False

    def _infer_set_count(self, session: dict[str, Any]) -> int | None:
        exercises = session.get("exercises") or session.get("Exercises")
        if isinstance(exercises, list):
            count = 0
            for exercise in exercises:
                if isinstance(exercise, dict):
                    sets = exercise.get("sets") or exercise.get("Sets")
                    if isinstance(sets, list):
                        count += len(sets)
            return count

        count = self._first_value(session, ["setCount", "numberOfSets", "setsCount"])
        if self._is_int_like(count):
            return int(count)
        return None

    def _find_run_list_in_nested_dict(self, data: Any) -> list:
        if isinstance(data, dict):
            for value in data.values():
                found = self._find_run_list_in_nested_dict(value)
                if found:
                    return found

        if isinstance(data, list):
            dict_items = [item for item in data if isinstance(item, dict)]
            if dict_items and any(self._looks_like_run_object(item) for item in dict_items):
                return dict_items
            for item in dict_items:
                found = self._find_run_list_in_nested_dict(item)
                if found:
                    return found

        return []

    @staticmethod
    def _looks_like_run_object(item: dict[str, Any]) -> bool:
        if not isinstance(item, dict):
            return False

        run_like_keys = {
            "run", "runNumber", "runIndex", "rep", "repNumber", "repetition",
            "distance", "distanceMeters", "totalDistance",
            "time", "duration", "durationSeconds", "totalTime",
            "topSpeed", "maxSpeed", "peakSpeed",
            "load", "resistance", "resistanceValues",
            "direction", "sampleData", "sample_data", "encodedData", "binaryData",
            "setDataId", "motionDataId", "trainingDataId", "motionGroups", "motions",
        }
        return any(key in item for key in run_like_keys)
