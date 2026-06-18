class AppError(Exception):
    status_code = 400
    code = "app_error"

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class UserNotFoundError(AppError):
    status_code = 404
    code = "user_not_found"

    def __init__(self, user_id: int):
        super().__init__(f"User with id={user_id} was not found.")
        self.user_id = user_id
