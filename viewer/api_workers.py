from __future__ import annotations

import json
import traceback
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

from viewer.motion_api import Motion1080Api, Motion1080ApiError
from viewer.run_library import RunIdentity, RunLibrary


class ApiWorkerError(RuntimeError):
    """
    Raised by API workers for user-facing background-task errors.
    """


class _ApiWorkerBase(QObject):
    """
    Common base for QThread API workers.
    """

    progress = Signal(str)
    error = Signal(str)

    def __init__(
        self,
        *,
        config_path: Path | None = None,
        timeout_seconds: float = 30.0,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.config_path = Path(config_path) if config_path is not None else None
        self.timeout_seconds = float(timeout_seconds)

    def _make_api(self) -> Motion1080Api:
        return Motion1080Api.from_environment_or_config(
            config_path=self.config_path,
            timeout_seconds=self.timeout_seconds,
        )

    def _emit_exception(self, exc: Exception, context: str) -> None:
        if isinstance(exc, (Motion1080ApiError, ApiWorkerError)):
            self.error.emit(str(exc))
            return

        self.error.emit(
            f"{context}\n\n"
            f"{type(exc).__name__}: {exc}\n\n"
            f"Traceback:\n{traceback.format_exc()}"
        )


class ApiFetchClientsWorker(_ApiWorkerBase):
    """
    Fetch all clients and normalize them for the sidebar.
    """

    finished = Signal(list)

    @Slot()
    def run(self) -> None:
        try:
            self.progress.emit("Connecting to 1080 API...")
            api = self._make_api()

            self.progress.emit("Fetching clients...")
            clients = api.normalize_clients(api.get_clients())

            self.progress.emit(f"Loaded {len(clients)} clients.")
            self.finished.emit(clients)

        except Exception as exc:
            self._emit_exception(exc, "Could not fetch clients from 1080 API.")


class ApiFetchSessionsWorker(_ApiWorkerBase):
    """
    Fetch sessions using /Session/Search and normalize them for the sidebar.
    """

    finished = Signal(list)

    def __init__(
        self,
        *,
        client_id: str | None = None,
        max_age_days: int = -1,
        extra_params: dict[str, Any] | None = None,
        config_path: Path | None = None,
        timeout_seconds: float = 30.0,
        parent: QObject | None = None,
    ):
        super().__init__(
            config_path=config_path,
            timeout_seconds=timeout_seconds,
            parent=parent,
        )
        self.client_id = str(client_id).strip() if client_id else None
        self.max_age_days = int(max_age_days)
        self.extra_params = dict(extra_params or {})

    @Slot()
    def run(self) -> None:
        try:
            api = self._make_api()

            self.progress.emit("Fetching sessions...")
            raw_sessions = api.get_sessions(
                max_age_days=self.max_age_days,
                client_id=self.client_id,
                extra_params=self.extra_params,
            )

            sessions = api.normalize_sessions(raw_sessions, client_id=self.client_id)

            self.progress.emit(f"Loaded {len(sessions)} sessions.")
            self.finished.emit(sessions)

        except Exception as exc:
            self._emit_exception(exc, "Could not fetch sessions from 1080 API.")


class ApiFetchSessionDetailsWorker(_ApiWorkerBase):
    """
    Fetch one session detail object and extract real sets from:
        session -> exercises[] -> sets[]
    """

    finished = Signal(dict)

    def __init__(
        self,
        *,
        session_id: str,
        fallback_session: dict[str, Any] | None = None,
        config_path: Path | None = None,
        timeout_seconds: float = 30.0,
        parent: QObject | None = None,
    ):
        super().__init__(
            config_path=config_path,
            timeout_seconds=timeout_seconds,
            parent=parent,
        )
        self.session_id = str(session_id or "").strip()
        self.fallback_session = dict(fallback_session or {})

    @Slot()
    def run(self) -> None:
        try:
            if not self.session_id:
                raise ApiWorkerError("No session id was provided.")

            api = self._make_api()

            self.progress.emit("Fetching session details...")
            try:
                session = api.get_session_details(self.session_id)
            except Exception:
                if self.fallback_session:
                    session = self.fallback_session
                else:
                    raise

            sets = api.normalize_sets_from_session(session)

            self.progress.emit(f"Loaded {len(sets)} sets.")
            self.finished.emit(
                {
                    "session": session,
                    "sets": sets,
                }
            )

        except Exception as exc:
            self._emit_exception(exc, "Could not fetch session details from 1080 API.")


class ApiFetchTrainingDataForSetWorker(_ApiWorkerBase):
    """
    Fetch TrainingData for one selected set and normalize motionGroups into runs.

    Correct endpoint:
        GET /TrainingData/Set/{setId}?includeSamples=true&filterMode=None
    """

    finished = Signal(dict)

    def __init__(
        self,
        *,
        set_id: str,
        include_samples: bool = True,
        filter_mode: str = "None",
        config_path: Path | None = None,
        timeout_seconds: float = 30.0,
        parent: QObject | None = None,
    ):
        super().__init__(
            config_path=config_path,
            timeout_seconds=timeout_seconds,
            parent=parent,
        )
        self.set_id = str(set_id or "").strip()
        self.include_samples = bool(include_samples)
        self.filter_mode = str(filter_mode or "None")

    @Slot()
    def run(self) -> None:
        try:
            if not self.set_id:
                raise ApiWorkerError("No set id was provided.")

            api = self._make_api()

            self.progress.emit("Fetching training data for selected set...")
            training_set = api.get_training_data_set(
                self.set_id,
                include_samples=self.include_samples,
                filter_mode=self.filter_mode,
            )

            self.progress.emit("Extracting runs...")
            runs = api.normalize_runs_from_training_set(training_set)

            self.progress.emit(f"Loaded {len(runs)} runs.")
            self.finished.emit(
                {
                    "endpoint": f"TrainingData/Set/{self.set_id}",
                    "training_data": training_set,
                    "runs": runs,
                }
            )

        except Exception as exc:
            self._emit_exception(exc, "Could not fetch training data for selected set.")


class ApiNormalizeRunsWorker(QObject):
    """
    Legacy worker: normalize runs from an already-fetched object.

    The corrected main-window flow should prefer ApiFetchTrainingDataForSetWorker.
    This class remains for backwards compatibility.
    """

    progress = Signal(str)
    finished = Signal(list)
    error = Signal(str)

    def __init__(
        self,
        *,
        set_obj: dict[str, Any],
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.set_obj = dict(set_obj or {})

    @Slot()
    def run(self) -> None:
        try:
            api = Motion1080Api(api_key="dummy-key-not-used")
            runs = api.normalize_runs_from_set(self.set_obj)
            self.progress.emit(f"Loaded {len(runs)} runs.")
            self.finished.emit(runs)
        except Exception as exc:
            self.error.emit(
                "Could not normalize runs.\n\n"
                f"{type(exc).__name__}: {exc}\n\n"
                f"Traceback:\n{traceback.format_exc()}"
            )


class ApiFetchRunDataWorker(_ApiWorkerBase):
    """
    Legacy/debug worker to fetch or decode raw run data.
    """

    finished = Signal(dict)

    def __init__(
        self,
        *,
        run_id: str,
        raw_run_data: dict[str, Any] | None = None,
        config_path: Path | None = None,
        timeout_seconds: float = 30.0,
        parent: QObject | None = None,
    ):
        super().__init__(
            config_path=config_path,
            timeout_seconds=timeout_seconds,
            parent=parent,
        )
        self.run_id = str(run_id or "").strip()
        self.raw_run_data = dict(raw_run_data or {})

    @Slot()
    def run(self) -> None:
        try:
            api = self._make_api()

            if self.raw_run_data:
                endpoint = "TrainingData/Set selected motionGroup"
                raw_data = self.raw_run_data
            else:
                if not self.run_id:
                    raise ApiWorkerError("No run id was provided.")
                self.progress.emit("Fetching run data...")
                endpoint, raw_data = api.fetch_run_data_candidates(self.run_id)

            self.progress.emit("Decoding samples...")
            decoded_samples = api.decode_all_samples_concatenated(raw_data)

            payload = {
                "endpoint": endpoint,
                "raw_data": raw_data,
                "sample_data_field_found": bool(decoded_samples.get("time")),
                "decoded_samples": decoded_samples,
            }

            self.progress.emit("Run data loaded.")
            self.finished.emit(payload)

        except Exception as exc:
            self._emit_exception(exc, "Could not fetch/decode run data from 1080 API.")


class ApiFetchAndCacheRunDataWorker(_ApiWorkerBase):
    """
    Decode samples for one selected run/motionGroup, export them to local Excel,
    and update RunLibrary metadata.

    The corrected flow should pass raw_run_data=self.selected_run['_raw'] from
    main_window.py. That raw object is a motionGroup containing motions/sampleData.
    """

    finished = Signal(dict)

    def __init__(
        self,
        *,
        identity: RunIdentity,
        raw_run_data: dict[str, Any] | None = None,
        config_path: Path | None = None,
        timeout_seconds: float = 30.0,
        library_root_dir: Path | None = None,
        parent: QObject | None = None,
    ):
        super().__init__(
            config_path=config_path,
            timeout_seconds=timeout_seconds,
            parent=parent,
        )
        self.identity = identity
        self.raw_run_data = dict(raw_run_data or {})
        self.library_root_dir = Path(library_root_dir) if library_root_dir is not None else None

    @Slot()
    def run(self) -> None:
        try:
            if not self.identity.run_id:
                raise ApiWorkerError("No run id was provided.")

            api = self._make_api()
            library = RunLibrary(root_dir=self.library_root_dir)

            if self.raw_run_data:
                self.progress.emit("Using samples from selected run...")
                endpoint = "TrainingData/Set selected motionGroup"
                raw_data = self.raw_run_data
            else:
                self.progress.emit("Fetching run data...")
                endpoint, raw_data = api.fetch_run_data_candidates(self.identity.run_id)

            run_dir = library.get_run_dir(self.identity)
            run_dir.mkdir(parents=True, exist_ok=True)

            raw_json_path = run_dir / "raw_run_data.json"
            with raw_json_path.open("w", encoding="utf-8") as f:
                json.dump(raw_data, f, indent=2, ensure_ascii=False)
                f.write("\n")

            self.progress.emit("Decoding samples...")
            decoded = api.decode_all_samples_concatenated(raw_data)

            if not decoded.get("time"):
                raise ApiWorkerError(
                    "No sampleData was found for the selected run. "
                    f"Raw JSON was saved here: {raw_json_path}"
                )

            self.progress.emit("Writing Excel data...")
            
            excel_path, rows = self._write_decoded_samples_to_excel(
                decoded_samples=decoded,
                excel_path=library.get_data_excel_path(self.identity),
                raw_run_data=raw_data,
            )


            # This updates the run metadata and completion state if video exists.
            library.copy_excel_file(self.identity, excel_path)
            meta = library.load_run_meta(self.identity) or {}

            payload = {
                "identity": self.identity.to_dict(),
                "endpoint": endpoint,
                "excel_path": str(excel_path),
                "raw_json_path": str(raw_json_path),
                "meta": meta,
                "rows": int(rows),
            }

            self.progress.emit(f"Saved {rows} samples to Excel.")
            self.finished.emit(payload)

        except Exception as exc:
            self._emit_exception(exc, "Could not fetch/cache run data from 1080 API.")

    def _write_decoded_samples_to_excel(
        self,
        *,
        decoded_samples: dict[str, list[float]],
        excel_path: Path,
        raw_run_data: dict[str, Any] | None = None,
    ) -> tuple[Path, int]:
        """
        Save decoded 1080 API samples to an Excel file that matches the original
        1080 Motion data-samples export structure.

        The important original-style sample columns are:

            Sample Distance [m]
            Sample Time [s]
            Speed [m/s]
            Acceleration [m/s2]
            Force [N]
            Power [W]

        Metadata columns are included before the sample columns, and metadata is
        written only on the first row, similar to the original 1080 export.
        """
        try:
            import pandas as pd
            import numpy as np
        except Exception as exc:
            raise ApiWorkerError(
                "pandas/openpyxl are required to export run data to Excel."
            ) from exc

        raw_run_data = dict(raw_run_data or {})

        required_columns = ["time", "position", "speed", "acceleration", "force"]
        lengths = [len(decoded_samples.get(col, [])) for col in required_columns]

        if not lengths or max(lengths) == 0:
            raise ApiWorkerError("Decoded sample data is empty.")

        row_count = min(lengths)

        if row_count <= 0:
            raise ApiWorkerError("Decoded sample data is empty.")

        sample_time = np.asarray(
            decoded_samples.get("time", [])[:row_count],
            dtype=float,
        )
        position = np.asarray(
            decoded_samples.get("position", [])[:row_count],
            dtype=float,
        )
        speed = np.asarray(
            decoded_samples.get("speed", [])[:row_count],
            dtype=float,
        )
        acceleration = np.asarray(
            decoded_samples.get("acceleration", [])[:row_count],
            dtype=float,
        )
        force = np.asarray(
            decoded_samples.get("force", [])[:row_count],
            dtype=float,
        )

        # The original 1080 export uses sample distance starting at 0.
        # API position can be absolute machine position, so normalize it.
        if position.size:
            sample_distance = position - position[0]
        else:
            sample_distance = position

        # Original export contains Power [W].
        power = speed * force

        # raw_run_data is normally a motionGroup:
        #
        # {
        #   "id": "...",
        #   "comment": "...",
        #   "side": "...",
        #   "motions": [...]
        # }
        motions = raw_run_data.get("motions") or []
        first_motion = motions[0] if motions and isinstance(motions[0], dict) else {}

        resistance = first_motion.get("resistanceValues") or {}
        if not isinstance(resistance, dict):
            resistance = {}

        created = (
            first_motion.get("created")
            or raw_run_data.get("created")
            or None
        )

        comment = raw_run_data.get("comment")
        side = raw_run_data.get("side")

        concentric_load = resistance.get("concentricLoad")
        eccentric_load = resistance.get("eccentricLoad")
        concentric_speed_limit = resistance.get("concentricSpeedLimit")
        eccentric_speed_limit = resistance.get("eccentricSpeedLimit")
        mode = resistance.get("mode")
        gear = resistance.get("gear")

        total_distance = first_motion.get("totalDistance")
        total_time = first_motion.get("totalTime")

        # Best-effort direction label.
        # If this turns out reversed for your specific data, we can flip it later.
        if first_motion.get("isEccentric") is True:
            direction = "Resisted"
        elif first_motion.get("isEccentric") is False:
            direction = "Assisted"
        else:
            direction = side if side and str(side).lower() != "unknown" else None

        # Build the same column structure as the original 1080 export.
        data = {
            "Client": [None] * row_count,
            "Client Weight [kg]": [None] * row_count,
            "Client Height [m]": [None] * row_count,
            "Group": [None] * row_count,
            "SessionTime": [None] * row_count,
            "SetTime": [None] * row_count,
            "RepTime": [None] * row_count,
            "Exercise": [None] * row_count,
            "ExerciseType": [None] * row_count,
            "MachineIndex": [None] * row_count,
            "SetNumber": [None] * row_count,
            "RepNumber": [None] * row_count,
            "Concentric Load [kg]": [None] * row_count,
            "Eccentric Load [kg]": [None] * row_count,
            "Concentric Speed Limit [m/s]": [None] * row_count,
            "Eccentric Speed Limit [m/s]": [None] * row_count,
            "Mode": [None] * row_count,
            "Gear": [None] * row_count,
            "Comment": [None] * row_count,
            "Distance [m]": [None] * row_count,
            "Time [s]": [None] * row_count,
            "Direction": [None] * row_count,
            "Sample Distance [m]": sample_distance,
            "Sample Time [s]": sample_time,
            "Speed [m/s]": speed,
            "Acceleration [m/s2]": acceleration,
            "Force [N]": force,
            "Power [W]": power,
        }

        # Fill metadata only on first row, matching the original 1080-style export.
        data["Client"][0] = self.identity.client_name or None
        data["SessionTime"][0] = created
        data["SetTime"][0] = created
        data["RepTime"][0] = created
        data["Exercise"][0] = self.identity.set_name or None
        data["ExerciseType"][0] = None
        data["MachineIndex"][0] = 1
        data["SetNumber"][0] = self.identity.set_index
        data["RepNumber"][0] = self.identity.run_index
        data["Concentric Load [kg]"][0] = concentric_load
        data["Eccentric Load [kg]"][0] = eccentric_load
        data["Concentric Speed Limit [m/s]"][0] = concentric_speed_limit
        data["Eccentric Speed Limit [m/s]"][0] = eccentric_speed_limit
        data["Mode"][0] = mode
        data["Gear"][0] = gear
        data["Comment"][0] = comment
        data["Distance [m]"][0] = total_distance
        data["Time [s]"][0] = total_time
        data["Direction"][0] = direction

        df = pd.DataFrame(data)

        excel_path = Path(excel_path)
        excel_path.parent.mkdir(parents=True, exist_ok=True)

        with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Sheet1")

        return excel_path, int(row_count)



class ApiValidateKeyWorker(_ApiWorkerBase):
    """
    Validate the configured API key by calling GET /Client.
    """

    finished = Signal(bool, str)

    @Slot()
    def run(self) -> None:
        try:
            self.progress.emit("Validating 1080 API key...")
            api = self._make_api()
            clients = api.get_clients()
            self.finished.emit(True, f"API key valid. Loaded {len(clients)} clients.")
        except Exception as exc:
            self._emit_exception(exc, "Could not validate 1080 API key.")