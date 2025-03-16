#!/usr/bin/env python3
import os
import sys
import json
import base64
import getpass
import traceback
import time

from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer
from github import Github, GithubException

# Configuration file where tokens are saved.
CONFIG_FILE = "tokens.json"


def load_config():
    """Load token configuration from the JSON file."""
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(config):
    """Save the token configuration to the JSON file."""
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)


def add_token():
    """Interactive mode to add a GitHub token to the configuration."""
    username = input("Enter GitHub username: ").strip()
    token_name = input("Enter token name (e.g., default, work, personal): ").strip()
    token = getpass.getpass("Enter GitHub token (input hidden): ").strip()
    config = load_config()
    if username not in config:
        config[username] = {}
    config[username][token_name] = token
    save_config(config)
    print(f"Token for user '{username}' with alias '{token_name}' saved successfully.")


# Define a custom virtual filesystem that does not expose the local filesystem.
from pyftpdlib.filesystems import AbstractedFS

class VirtualGitHubFS(AbstractedFS):
    def __init__(self, root, cmd_channel):
        self.root = "/"
        self.cwd = "/"
        self.cmd_channel = cmd_channel

    def ftp2fs(self, ftppath):
        if not ftppath.startswith("/"):
            ftppath = "/" + ftppath
        return ftppath

    def fs2ftp(self, fspath):
        return fspath
    
    def validpath(self, path):
        return True

    def getcwd(self):
        return self.cwd

    def chdir(self, path):
        self.cwd = path
        self.cmd_channel.current_path = path
        return True
    
    def listdir(self, path):
        return []
    
    def isfile(self, path):
        return True
    
    def isdir(self, path):
        return True
    
    def getsize(self, path):
        return 0
    
    def getmtime(self, path):
        import time
        return time.time()
    
    def mkdir(self, path):
        raise NotImplementedError("Creating directories is not supported")
    
    def rmdir(self, path):
        raise NotImplementedError("Removing directories is not supported")
    
    def remove(self, path):
        raise NotImplementedError("File removal to be handled by FTP handler")
    
    def rename(self, src, dst):
        raise NotImplementedError("Renaming is not supported")


class GitHubFTPHandler(FTPHandler):
    """
    Custom FTP handler that translates FTP commands into GitHub API actions.
    The virtual filesystem works like this:
      - "/" (root) lists all repositories for the GitHub user.
      - "/repo_name/..." represents paths inside a repository.
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.github = None
        self.github_username = None
        self.token_alias = None
        self.config = {}
        self.authenticated = False
        self.current_path = None
        self.fs = None

    def on_connect(self):
        self.current_path = "/"
        self.fs = VirtualGitHubFS("/", self)
        self.authenticated = False
        self.github = None
        self.github_username = None
        self.token_alias = None
        self.config = {}
        print(f"[DEBUG] New connection established, current_path: {self.current_path}")

    def ftp_USER(self, username):
        self.username = username
        self.github_username = username
        self.config = load_config()
        print(f"[DEBUG] USER command received: {username}")

        if self.github_username not in self.config:
            self.respond("530 No token configuration found for this username. "
                         "Please add a token using the addtoken command.")
            self.close_when_done()
            return

        if username not in self.authorizer.user_table:
            self.authorizer.add_user(username, "ignored", "/", perm="elradfmw")
        self.respond("331 Username ok. For the PASS command, provide the token alias "
                     "(if multiple tokens exist) or send an empty password if only one token is saved.")

    def ftp_PASS(self, token_alias):
        print(f"[DEBUG] PASS command received with token alias: {token_alias}")
        tokens = self.config.get(self.github_username, {})
        if not tokens:
            self.respond("530 No token configuration found.")
            self.close_when_done()
            return

        if token_alias == "":
            if len(tokens) == 1:
                token = list(tokens.values())[0]
                used_token_alias = list(tokens.keys())[0]
            else:
                self.respond("530 Multiple tokens available. Please specify the token alias as your password.")
                self.close_when_done()
                return
        else:
            if token_alias in tokens:
                token = tokens[token_alias]
                used_token_alias = token_alias
            else:
                self.respond("530 Token alias not found for this user.")
                self.close_when_done()
                return

        try:
            self.github = Github(token)
            user = self.github.get_user().login
            self.authenticated = True
            self.token_alias = used_token_alias
            self.respond(f"230 Login successful as {user} (token alias: {used_token_alias}).")
            print(f"[DEBUG] Authentication successful for {user} with alias {used_token_alias}")
        except GithubException as e:
            self.respond(f"530 Login incorrect: {str(e)}")
            self.close_when_done()
            print(f"[DEBUG] Authentication failed: {str(e)}")

    def ftp_LIST(self, path):
        if not self.authenticated:
            self.respond("530 Please login first.")
            return

        if self.data_channel is not None:
            try:
                self.data_channel.flush()
            except Exception:
                pass
            
        self.respond("150 File status okay; about to open data connection.")
        print(f"[DEBUG] LIST command for path: {path}")
        
        try:
            listing = []
            full_path = self._resolve_path(path)
            print(f"[DEBUG] Resolved path: {full_path}")
            
            if full_path == "/":
                repos = self.github.get_user().get_repos()
                for repo in repos:
                    listing.append(f"drwxr-xr-x 1 {self.github_username} {self.github_username} 0 Jan 1 00:00 {repo.name}")
            else:
                parts = full_path.strip("/").split("/", 1)
                repo_name = parts[0]
                repo_path = parts[1] if len(parts) > 1 else ""
                print(f"[DEBUG] Repo: {repo_name}, Path: {repo_path}")
                
                try:
                    repo = self.github.get_user().get_repo(repo_name)
                    contents = repo.get_contents(repo_path)
                    
                    if not isinstance(contents, list):
                        contents = [contents]
                        
                    for item in contents:
                        file_type = "d" if item.type == "dir" else "-"
                        listing.append(f"{file_type}rwxr-xr-x 1 {self.github_username} {self.github_username} {item.size} Jan 1 00:00 {item.name}")
                except GithubException as e:
                    if e.status == 404 and "This repository is empty" in str(e):
                        print(f"[DEBUG] Repository {repo_name} is empty, returning empty listing")
                        listing = []
                    else:
                        self.respond(f"550 Failed to list directory: {str(e)}")
                        print(f"[DEBUG] GitHub error: {str(e)}")
                        return
            
            if self.data_channel is not None:
                print(f"[DEBUG] Sending listing: {listing}")
                self.push_dtp_data("\r\n".join(listing).encode('utf-8', 'replace'))
                self.respond("226 Directory send OK.")
            else:
                self.respond("425 Can't open data connection.")
                print("[DEBUG] Data channel is None")
                
        except Exception as e:
            self.respond(f"550 Failed to list directory: {str(e)}")
            print(f"[DEBUG] Exception: {str(e)}")
            if self.data_channel is not None:
                self.data_channel.close()

    def ftp_MLSD(self, path):
        if not self.authenticated:
            self.respond("530 Please login first.")
            return

        self.respond("150 File status okay; about to open data connection.")
        print(f"[DEBUG] MLSD command for path: {path}")
        
        try:
            listing = []
            full_path = self._resolve_path(path)
            print(f"[DEBUG] Resolved path: {full_path}")
            
            if full_path == "/":
                repos = self.github.get_user().get_repos()
                for repo in repos:
                    listing.append(f"type=dir;size=0;modify=19700101000000; {repo.name}")
            else:
                parts = full_path.strip("/").split("/", 1)
                repo_name = parts[0]
                repo_path = parts[1] if len(parts) > 1 else ""
                print(f"[DEBUG] Repo: {repo_name}, Path: {repo_path}")
                
                try:
                    repo = self.github.get_user().get_repo(repo_name)
                    contents = repo.get_contents(repo_path)
                    
                    if not isinstance(contents, list):
                        contents = [contents]
                        
                    for item in contents:
                        item_type = "dir" if item.type == "dir" else "file"
                        listing.append(f"type={item_type};size={item.size};modify=19700101000000; {item.name}")
                except GithubException as e:
                    if e.status == 404 and "This repository is empty" in str(e):
                        print(f"[DEBUG] Repository {repo_name} is empty, returning empty listing")
                        listing = []
                    else:
                        self.respond(f"550 Failed to list directory: {str(e)}")
                        print(f"[DEBUG] GitHub error: {str(e)}")
                        return
            
            if self.data_channel is not None:
                print(f"[DEBUG] Sending MLSD listing: {listing}")
                self.push_dtp_data("\r\n".join(listing).encode('utf-8', 'replace'))
                self.respond("226 Directory send OK.")
            else:
                self.respond("425 Can't open data connection.")
                print("[DEBUG] Data channel is None")
                
        except Exception as e:
            self.respond(f"550 Failed to list directory: {str(e)}")
            print(f"[DEBUG] Exception: {str(e)}")
            if self.data_channel is not None:
                self.data_channel.close()

    def ftp_PWD(self, line):
        self.respond(f'257 "{self.current_path}" is the current directory.')
        print(f"[DEBUG] PWD command, current_path: {self.current_path}")

    def ftp_CWD(self, path):
        if not self.authenticated:
            self.respond("530 Please login first.")
            return
            
        print(f"[DEBUG] CWD command for path: {path}")
        try:
            if path == ".":
                self.respond("250 Directory unchanged.")
                return
            elif path == "..":
                if self.current_path == "/":
                    self.respond("250 Directory unchanged (already at root).")
                    return
                else:
                    parts = self.current_path.rstrip("/").split("/")
                    new_path = "/" + "/".join(parts[:-1])
                    if new_path == "":
                        new_path = "/"
            else:
                if path.startswith("/"):
                    new_path = path
                else:
                    if self.current_path.endswith("/"):
                        new_path = self.current_path + path
                    else:
                        new_path = self.current_path + "/" + path
                
                if new_path != "/":
                    new_path = new_path.rstrip("/")
            
            if new_path == "/":
                pass
            else:
                parts = new_path.strip("/").split("/", 1)
                repo_name = parts[0]
                repo_path = parts[1] if len(parts) > 1 else ""
                print(f"[DEBUG] Changing to Repo: {repo_name}, Path: {repo_path}")
                
                try:
                    repo = self.github.get_user().get_repo(repo_name)
                    if repo_path:
                        contents = repo.get_contents(repo_path)
                        if not contents.type == "dir":
                            self.respond("550 Not a directory.")
                            return
                except GithubException as e:
                    self.respond(f"550 Directory not found: {str(e)}")
                    print(f"[DEBUG] GitHub error: {str(e)}")
                    return
            
            self.current_path = new_path
            self.fs.chdir(new_path)
            self.respond("250 Directory successfully changed.")
            print(f"[DEBUG] Directory changed to: {new_path}")
            
        except Exception as e:
            self.respond(f"550 Failed to change directory: {str(e)}")
            print(f"[DEBUG] CWD exception: {str(e)}")

    def ftp_RETR(self, filename):
        if not self.authenticated:
            self.respond("530 Please login first.")
            return

        print(f"[DEBUG] RETR command for file: {filename}")
        try:
            full_path = self._resolve_path(filename)
            parts = full_path.strip("/").split("/", 1)
            
            if len(parts) < 2:
                self.respond("550 Please specify a file inside a repository.")
                return
                
            repo_name = parts[0]
            file_path = parts[1]
            print(f"[DEBUG] Retrieving from Repo: {repo_name}, File: {file_path}")
            
            repo = self.github.get_user().get_repo(repo_name)
            file_content = repo.get_contents(file_path)
            
            if file_content.type == "dir":
                self.respond("550 Path is a directory, not a file.")
                return
                
            data = base64.b64decode(file_content.content)
            
            self.respond("150 Opening data connection for file download.")
            self.push_dtp_data(data)
            self.respond("226 Transfer complete.")
            print("[DEBUG] File transfer completed")
            
        except GithubException as e:
            self.respond(f"550 Failed to retrieve file: {str(e)}")
            print(f"[DEBUG] GitHub error: {str(e)}")
        except Exception as e:
            self.respond(f"550 Error during file transfer: {str(e)}")
            print(f"[DEBUG] RETR exception: {str(e)}")

    def ftp_STOR(self, filename):
        if not self.authenticated:
            self.respond("530 Please login first.")
            return

        print(f"[DEBUG] STOR command for file: {filename}")
        try:
            # Resolve the path, adjusting for absolute paths that lack a repo
            if filename.startswith("/"):
                full_path = filename
                parts = full_path.strip("/").split("/", 1)
                if len(parts) < 2 or not parts[0]:  # e.g., "/file.jpg" or "/"
                    if self.current_path != "/":
                        repo_name = self.current_path.strip("/").split("/")[0]
                        full_path = f"/{repo_name}/{filename.lstrip('/')}"
                    else:
                        self.respond("550 Please specify a repository (e.g., /repo_name/file) when at root.")
                        return
            else:
                full_path = self._resolve_path(filename)
            
            parts = full_path.strip("/").split("/", 1)
            if len(parts) < 2:
                self.respond("550 Invalid path: must include a repository and file (e.g., /repo_name/file).")
                return
                
            repo_name = parts[0]
            file_path = parts[1]
            print(f"[DEBUG] Storing to Repo: {repo_name}, File: {file_path}")
            
            self.respond("150 Opening data connection for file upload.")
            # Wait briefly to ensure the data channel is ready
            time.sleep(0.1)
            data = self.get_dtp_data()
            print(f"[DEBUG] Received data size: {len(data)} bytes")
            
            repo = self.github.get_user().get_repo(repo_name)
            commit_message = f"FTP upload: {file_path}"
            
            try:
                file_info = repo.get_contents(file_path)
                print(f"[DEBUG] Updating existing file: {file_path}")
                repo.update_file(file_path, commit_message, data, file_info.sha)
            except GithubException as e:
                if e.status == 404:  # File doesn't exist, create it
                    print(f"[DEBUG] Creating new file: {file_path}")
                    repo.create_file(file_path, commit_message, data)
                else:
                    raise
            
            self.respond("226 Transfer complete. File stored successfully.")
            print("[DEBUG] File upload completed")
            
        except GithubException as e:
            self.respond(f"550 Failed to store file: {str(e)}")
            print(f"[DEBUG] GitHub error: {str(e)}")
            traceback.print_exc()
        except Exception as e:
            self.respond(f"550 Error during file transfer: {str(e)}")
            print(f"[DEBUG] STOR exception: {str(e)}")
            traceback.print_exc()

    def get_dtp_data(self):
        if not self.data_channel:
            raise Exception("Data channel not established")
        
        data = bytearray()
        timeout = 10  # seconds to wait for data
        start_time = time.time()
        
        while True:
            try:
                chunk = self.data_channel.recv(1024)
                if chunk:
                    data.extend(chunk)
                    print(f"[DEBUG] Received chunk of {len(chunk)} bytes")
                elif chunk == b"":  # Empty chunk signals end of transfer
                    print("[DEBUG] End of data received")
                    break
            except BlockingIOError:
                # Wait briefly and check timeout
                if time.time() - start_time > timeout:
                    raise Exception("Timed out waiting for data from client")
                time.sleep(0.01)  # Short sleep to avoid busy-waiting
                continue
            except Exception as e:
                print(f"[DEBUG] Error in get_dtp_data: {str(e)}")
                raise
            finally:
                if not chunk and (time.time() - start_time > timeout):
                    raise Exception("No data received within timeout period")
        
        if self.data_channel:
            self.data_channel.close()
            self.data_channel = None
            print("[DEBUG] Data channel closed in get_dtp_data")
        
        return bytes(data)
    
    def push_dtp_data(self, data, isproducer=False):
        print(f"[DEBUG] Pushing data, isproducer: {isproducer}")
        if self.data_channel is None:
            print("[DEBUG] Data channel is None, cannot send data")
            return
        try:
            if isproducer:
                for chunk in data:
                    self.data_channel.send(chunk)
            else:
                self.data_channel.send(data)
        finally:
            self.data_channel.close()
            self.data_channel = None
            print("[DEBUG] Data channel closed")

    def _resolve_path(self, path):
        if path.startswith("/"):
            return path
        else:
            if self.current_path == "/":
                return "/" + path
            else:
                return self.current_path + "/" + path

    def ftp_DELE(self, path):
        if not self.authenticated:
            self.respond("530 Please login first.")
            return

        print(f"[DEBUG] DELE command for file: {path}")
        try:
            full_path = self._resolve_path(path)
            parts = full_path.strip("/").split("/", 1)
            
            if len(parts) < 2:
                self.respond("550 Please specify a file inside a repository.")
                return
                
            repo_name = parts[0]
            file_path = parts[1]
            print(f"[DEBUG] Deleting from Repo: {repo_name}, File: {file_path}")
            
            repo = self.github.get_user().get_repo(repo_name)
            file_content = repo.get_contents(file_path)
            
            if file_content.type == "dir":
                self.respond("550 Cannot delete a directory.")
                return
                
            repo.delete_file(file_path, f"FTP delete: {path}", file_content.sha)
            self.respond("250 File successfully deleted.")
            print("[DEBUG] File deletion completed")
            
        except GithubException as e:
            self.respond(f"550 Failed to delete file: {str(e)}")
            print(f"[DEBUG] GitHub error: {str(e)}")
        except Exception as e:
            self.respond(f"550 Error during file deletion: {str(e)}")
            print(f"[DEBUG] DELE exception: {str(e)}")

    def ftp_MKD(self, path):
        if not self.authenticated:
            self.respond("530 Please login first.")
            return

        print(f"[DEBUG] MKD command for path: {path}")
        try:
            full_path = self._resolve_path(path)
            parts = full_path.strip("/").split("/", 1)
            
            if len(parts) < 2:
                self.respond("550 Please specify a directory inside a repository.")
                return
                
            repo_name = parts[0]
            dir_path = parts[1]
            gitkeep_path = dir_path + "/.gitkeep" if dir_path.endswith("/") else dir_path + "/.gitkeep"
            print(f"[DEBUG] Creating dir in Repo: {repo_name}, Path: {gitkeep_path}")
            
            repo = self.github.get_user().get_repo(repo_name)
            commit_message = f"FTP mkdir: {path}"
            
            repo.create_file(gitkeep_path, commit_message, "")
            self.respond(f'257 "{path}" directory created.')
            print("[DEBUG] Directory creation completed")
            
        except GithubException as e:
            self.respond(f"550 Failed to create directory: {str(e)}")
            print(f"[DEBUG] GitHub error: {str(e)}")
        except Exception as e:
            self.respond(f"550 Error during directory creation: {str(e)}")
            print(f"[DEBUG] MKD exception: {str(e)}")

    def ftp_RMD(self, path):
        if not self.authenticated:
            self.respond("530 Please login first.")
            return

        print(f"[DEBUG] RMD command for path: {path}")
        try:
            full_path = self._resolve_path(path)
            parts = full_path.strip("/").split("/", 1)
            
            if len(parts) < 2:
                self.respond("550 Cannot delete a repository through FTP. Use GitHub's web interface.")
                return
                
            repo_name = parts[0]
            dir_path = parts[1]
            if not dir_path.endswith("/"):
                dir_path += "/"
            print(f"[DEBUG] Removing dir from Repo: {repo_name}, Path: {dir_path}")
            
            repo = self.github.get_user().get_repo(repo_name)
            contents = repo.get_contents(dir_path)
            
            if not isinstance(contents, list):
                contents = [contents]
                
            for item in contents:
                if item.type == "file":
                    repo.delete_file(item.path, f"FTP rmdir: deleting {item.name}", item.sha)
                else:
                    self.respond(f"550 Cannot recursively delete directories. Please delete files in {item.path} first.")
                    return
                    
            self.respond("250 Directory successfully removed.")
            print("[DEBUG] Directory removal completed")
            
        except GithubException as e:
            self.respond(f"550 Failed to remove directory: {str(e)}")
            print(f"[DEBUG] GitHub error: {str(e)}")
        except Exception as e:
            self.respond(f"550 Error during file removal: {str(e)}")
            print(f"[DEBUG] RMD exception: {str(e)}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "addtoken":
        add_token()
        return

    authorizer = DummyAuthorizer()
    handler = GitHubFTPHandler
    handler.authorizer = authorizer

    server = FTPServer(("0.0.0.0", 8021), handler)
    server.max_cons = 256
    server.max_cons_per_ip = 5
    server.passive_ports = range(60000, 65535)

    print("GitHub FTP Server starting on 0.0.0.0:8021")
    print("Use 'python script.py addtoken' to add or update your GitHub tokens")
    print("Connect with any FTP client using:")
    print("  - Username: Your GitHub username")
    print("  - Password: Your token alias from tokens.json")
    print("Press Ctrl+C to stop the server")
    print(f"[DEBUG] Passive ports configured: {server.passive_ports}")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer shutting down")
        server.close_all()


if __name__ == '__main__':
    main()
