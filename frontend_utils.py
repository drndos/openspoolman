def color_is_dark(bg_color):
  # Accept only normalized six-digit hex colors (with or without '#'); everything else is safe to treat as light.
  if not isinstance(bg_color, str):
    return False

  color = bg_color.strip()
  color = color[1:] if color.startswith('#') else color

  if len(color) != 6:
    return False

  try:
    r = int(color[0:2], 16)  # Hex to R
    g = int(color[2:4], 16)  # Hex to G
    b = int(color[4:6], 16)  # Hex to B
  except ValueError:
    return False

  # Convert RGB values to normalized UI colors
  uicolors = [r / 255.0, g / 255.0, b / 255.0]

  # Apply formula to calculate perceived luminance
  c = [
    col / 12.92 if col <= 0.03928 else ((col + 0.055) / 1.055) ** 2.4
    for col in uicolors
  ]

  # Calculate luminance
  luminance = (0.2126 * c[0]) + (0.7152 * c[1]) + (0.0722 * c[2])

  # Return whether the color is considered "dark"
  return luminance <= 0.179
